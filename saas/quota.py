"""Quota accounting and enforcement.

Usage is derived from `llm_calls` rather than kept in a counter. Every call is
already recorded there, so there is one source of truth and no way for a counter
to drift away from what actually happened — which matters when the number decides
whether someone gets billed.

Checks run before a job is queued (so the user gets a clear message instead of a
job that dies) and again before each unit executes (so a long job cannot sail past
the ceiling once it has started).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from . import schema as s
from .engine import get_engine
from .plans import Plan, global_daily_run_cap, plan_for


class QuotaExceeded(RuntimeError):
    """Raised with a message intended to be shown to the user verbatim."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def period_start(now: datetime | None = None) -> datetime:
    """Start of the current calendar month, in UTC."""
    now = now or _now()
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def runs_used(user_id: int) -> int:
    with get_engine().begin() as c:
        return int(
            c.execute(
                select(func.count()).select_from(s.llm_calls).where(
                    s.llm_calls.c.user_id == user_id,
                    s.llm_calls.c.called_at >= period_start(),
                )
            ).scalar_one()
        )


def global_runs_today() -> int:
    with get_engine().begin() as c:
        return int(
            c.execute(
                select(func.count()).select_from(s.llm_calls).where(
                    s.llm_calls.c.called_at >= _now() - timedelta(days=1)
                )
            ).scalar_one()
        )


def user_plan(user: dict) -> Plan:
    """Effective plan, downgrading automatically once a subscription lapses.

    Expiry is checked on read rather than by a scheduled job: there is no window
    in which a lapsed subscriber still has Pro because a cron did not fire.
    """
    expires = user.get("plan_expires_at")
    if user.get("plan") == "pro":
        if expires is None:
            return plan_for("pro")
        if isinstance(expires, str):
            expires = datetime.fromisoformat(expires)
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if expires > _now():
            return plan_for("pro")
        return plan_for("free")
    return plan_for(user.get("plan"))


def status(user: dict) -> dict:
    """Quota state for the UI.

    Exposed twice under two names: `credits_*` is what the interface shows, and
    `runs_*` is the older internal name kept so nothing silently breaks. "Agent
    run" was never a phrase a job seeker should have had to learn.
    """
    plan = user_plan(user)
    used = runs_used(user["id"])
    left = max(0, plan.monthly_runs - used)
    return {
        "plan": plan.key,
        "plan_name": plan.name,
        "price_inr": plan.price_inr,
        "credits_used": used,
        "credits_limit": plan.monthly_runs,
        "credits_left": left,
        "runs_used": used,
        "runs_limit": plan.monthly_runs,
        "runs_left": left,
        "max_scores_per_batch": plan.max_scores_per_batch,
        "blocked_agents": sorted(plan.blocked_agents),
        "resets_on": (period_start() + timedelta(days=32)).replace(day=1).date().isoformat(),
    }


def check(user: dict, kind: str, units: int = 1) -> None:
    """Raise QuotaExceeded if this work is not allowed. Messages are user-facing."""
    plan = user_plan(user)

    if kind in plan.blocked_agents:
        raise QuotaExceeded(
            "That is a Pro feature. Pro unlocks tailored resumes, outreach messages, "
            "interview prep, company briefings and salary bands."
        )

    used = runs_used(user["id"])
    if used + units > plan.monthly_runs:
        left = max(0, plan.monthly_runs - used)
        if plan.key == "free":
            raise QuotaExceeded(
                f"You have used {used} of your {plan.monthly_runs} free credits this "
                f"month ({left} left). Pro gives you 800 credits a month."
            )
        raise QuotaExceeded(
            f"You have used all {plan.monthly_runs} credits this month. "
            "They reset on the 1st."
        )

    cap = global_daily_run_cap()
    if global_runs_today() + units > cap:
        # Deliberately vague to the user, loud in the logs: this is an operator
        # problem, not something they did.
        print(f"GLOBAL DAILY RUN CAP HIT ({cap}) — refusing new work", flush=True)
        raise QuotaExceeded(
            "The service is at its daily capacity. Please try again in a few hours."
        )


def batch_limit(user: dict, requested: int) -> int:
    """Clamp a scoring batch to what the plan and remaining allowance permit."""
    plan = user_plan(user)
    remaining = max(0, plan.monthly_runs - runs_used(user["id"]))
    return max(0, min(requested, plan.max_scores_per_batch, remaining))
