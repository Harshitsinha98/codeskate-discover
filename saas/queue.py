"""Database-backed job queue with chunked execution.

Why this exists: the single-user app kept task state in a Python dict and ran work
on a thread. Neither survives serverless. Function instances are stateless, so the
request that polls for progress may reach a different instance than the one doing
the work, and any instance can be frozen or discarded between requests.

So state lives in Postgres, and work is split into units small enough to finish
inside a function timeout:

  enqueue()   plans the job — runs only free, fast work (reading config, applying
              the rule-based prefilter) and stores the resulting unit list
  work()      claims a job and executes units until its time budget runs low,
              committing progress after each one

A unit is the smallest useful step: one company board to fetch, one posting to
score, one LLM call. Because progress is committed per unit, a function that gets
killed mid-job loses one unit rather than the whole run, and the next worker tick
resumes where it stopped.

Claiming uses optimistic locking (update guarded by the state we read) rather than
SELECT FOR UPDATE, so the same code runs on SQLite and Postgres.
"""

from __future__ import annotations

import time
import traceback
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import and_, or_, select

from . import schema as sc
from .engine import get_engine

# A unit that has been running longer than this is assumed dead — the function
# hosting it was probably killed — and becomes claimable again.
STALE_LOCK = timedelta(minutes=5)
MAX_ATTEMPTS = 3

# Handlers are registered by handlers.py to avoid a circular import.
HANDLERS: dict[str, Callable[..., dict]] = {}
PLANNERS: dict[str, Callable[..., dict]] = {}


def register(kind: str, handler: Callable[..., dict], planner: Callable[..., dict] | None = None):
    HANDLERS[kind] = handler
    if planner:
        PLANNERS[kind] = planner


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# enqueue
# --------------------------------------------------------------------------- #


def enqueue(user_id: int, kind: str, label: str, payload: dict | None = None) -> dict:
    """Plan and queue a job. Planning must stay free and fast — it runs inline."""
    if kind not in HANDLERS:
        raise ValueError(f"unknown job kind: {kind}")

    payload = dict(payload or {})
    log: list[str] = []

    planner = PLANNERS.get(kind)
    if planner:
        plan = planner(user_id, payload)
        payload.update(plan.get("payload", {}))
        log.extend(plan.get("log", []))

    units = payload.get("units")
    units_total = len(units) if isinstance(units, list) else 1

    if isinstance(units, list) and not units:
        # Nothing to do — record it as finished rather than leaving a job that
        # can never make progress.
        with get_engine().begin() as c:
            result = c.execute(
                sc.queue_jobs.insert().values(
                    user_id=user_id, kind=kind, label=label, state="done",
                    payload=payload, log=log + ["nothing to do"], result={"units": 0},
                    units_done=0, units_total=0, created_at=_now(), updated_at=_now(),
                )
            )
            return {"id": int(result.inserted_primary_key[0]), "state": "done"}

    with get_engine().begin() as c:
        result = c.execute(
            sc.queue_jobs.insert().values(
                user_id=user_id, kind=kind, label=label, state="queued",
                payload=payload, log=log, units_done=0, units_total=units_total,
                created_at=_now(), updated_at=_now(),
            )
        )
    return {"id": int(result.inserted_primary_key[0]), "state": "queued"}


# --------------------------------------------------------------------------- #
# read
# --------------------------------------------------------------------------- #


def get_job(user_id: int, job_id: int) -> dict | None:
    """Scoped by user — a job id from another tenant must not be readable."""
    with get_engine().begin() as c:
        row = c.execute(
            select(sc.queue_jobs).where(
                and_(sc.queue_jobs.c.id == job_id, sc.queue_jobs.c.user_id == user_id)
            )
        ).first()
    if not row:
        return None
    job = dict(row._mapping)
    job["progress"] = (
        round(job["units_done"] / job["units_total"] * 100) if job["units_total"] else 100
    )
    return job


def active_jobs(user_id: int) -> list[dict]:
    with get_engine().begin() as c:
        rows = c.execute(
            select(sc.queue_jobs)
            .where(
                and_(
                    sc.queue_jobs.c.user_id == user_id,
                    sc.queue_jobs.c.state.in_(["queued", "running"]),
                )
            )
            .order_by(sc.queue_jobs.c.id)
        ).all()
    return [dict(r._mapping) for r in rows]


# --------------------------------------------------------------------------- #
# claim and execute
# --------------------------------------------------------------------------- #


def _claim() -> dict | None:
    """Take ownership of one runnable job, or return None.

    Optimistic locking: read a candidate, then update it guarded by the state we
    saw. If another worker won the race, the update affects zero rows and we
    simply try again.
    """
    engine = get_engine()
    cutoff = _now() - STALE_LOCK

    for _ in range(5):
        with engine.begin() as c:
            row = c.execute(
                select(sc.queue_jobs.c.id, sc.queue_jobs.c.state, sc.queue_jobs.c.attempts)
                .where(
                    or_(
                        sc.queue_jobs.c.state == "queued",
                        and_(
                            sc.queue_jobs.c.state == "running",
                            sc.queue_jobs.c.locked_at < cutoff,
                        ),
                    )
                )
                .order_by(sc.queue_jobs.c.id)
                .limit(1)
            ).first()

            if row is None:
                return None

            job_id, seen_state, attempts = row[0], row[1], row[2]
            if attempts >= MAX_ATTEMPTS:
                c.execute(
                    sc.queue_jobs.update()
                    .where(sc.queue_jobs.c.id == job_id)
                    .values(state="error", error=f"gave up after {attempts} attempts",
                            updated_at=_now())
                )
                continue

            updated = c.execute(
                sc.queue_jobs.update()
                .where(
                    and_(sc.queue_jobs.c.id == job_id, sc.queue_jobs.c.state == seen_state)
                )
                .values(state="running", locked_at=_now(), attempts=attempts + 1,
                        updated_at=_now())
            ).rowcount

            if updated == 1:
                claimed = c.execute(
                    select(sc.queue_jobs).where(sc.queue_jobs.c.id == job_id)
                ).first()
                return dict(claimed._mapping)

    return None


def _append_log(job_id: int, lines: list[str]) -> None:
    if not lines:
        return
    with get_engine().begin() as c:
        current = c.execute(
            select(sc.queue_jobs.c.log).where(sc.queue_jobs.c.id == job_id)
        ).scalar_one()
        c.execute(
            sc.queue_jobs.update()
            .where(sc.queue_jobs.c.id == job_id)
            .values(log=list(current or []) + lines, updated_at=_now())
        )


def work(budget_seconds: float = 45.0, max_jobs: int = 5) -> dict:
    """Process queued work until the time budget is nearly spent.

    The budget must sit comfortably below the platform's function timeout: the
    worker stops cleanly and the next tick resumes, rather than being killed
    part-way through a unit.
    """
    started = time.monotonic()
    processed_units = 0
    finished_jobs = 0
    touched: list[int] = []

    while time.monotonic() - started < budget_seconds and finished_jobs < max_jobs:
        job = _claim()
        if job is None:
            break

        touched.append(job["id"])
        handler = HANDLERS[job["kind"]]
        units = job["payload"].get("units")
        total = len(units) if isinstance(units, list) else 1

        while job["units_done"] < total:
            if time.monotonic() - started >= budget_seconds:
                # Hand the job back so another tick can continue it.
                with get_engine().begin() as c:
                    c.execute(
                        sc.queue_jobs.update()
                        .where(sc.queue_jobs.c.id == job["id"])
                        .values(state="queued", locked_at=None, updated_at=_now())
                    )
                return {
                    "units": processed_units, "jobs": finished_jobs,
                    "job_ids": touched, "yielded": True,
                }

            index = job["units_done"]
            try:
                outcome = handler(job["user_id"], job["payload"], index)
            except Exception as e:  # noqa: BLE001
                traceback.print_exc()
                with get_engine().begin() as c:
                    c.execute(
                        sc.queue_jobs.update()
                        .where(sc.queue_jobs.c.id == job["id"])
                        .values(state="error", error=f"{type(e).__name__}: {e}",
                                locked_at=None, updated_at=_now())
                    )
                finished_jobs += 1
                break

            processed_units += 1
            job["units_done"] = index + 1
            _append_log(job["id"], outcome.get("log", []))

            with get_engine().begin() as c:
                values: dict[str, Any] = {
                    "units_done": job["units_done"], "updated_at": _now(),
                    "locked_at": _now(),
                }
                if job["units_done"] >= total:
                    values["state"] = "done"
                    values["locked_at"] = None
                    if outcome.get("result") is not None:
                        values["result"] = outcome["result"]
                elif outcome.get("result") is not None:
                    values["result"] = outcome["result"]
                c.execute(
                    sc.queue_jobs.update()
                    .where(sc.queue_jobs.c.id == job["id"])
                    .values(**values)
                )

            if job["units_done"] >= total:
                finished_jobs += 1
                break

    return {"units": processed_units, "jobs": finished_jobs, "job_ids": touched,
            "yielded": False}
