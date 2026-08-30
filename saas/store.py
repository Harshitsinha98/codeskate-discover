"""Data access. Every function that touches per-user data takes user_id first.

There is deliberately no unscoped accessor for per-user tables. Tenant isolation
enforced by convention leaks the first time someone is in a hurry; making the
scope a required positional argument means a missing tenant filter is a
TypeError, not a data breach.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from sqlalchemy import and_, delete, desc, func, select, update

from . import schema as s
from .engine import get_engine, upsert, upsert_nothing


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _rows(result) -> list[dict]:
    return [dict(r._mapping) for r in result]


# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #


def upsert_google_user(sub: str, email: str, name: str, picture: str | None) -> dict:
    """Find or create the account behind a Google identity.

    Matched on `sub` first and email second: Google's subject id is stable, while
    the email on a Google account can change. Matching on email alone would strand
    a user who renamed their address, and matching only on sub would create a
    duplicate for anyone who existed before this column did.
    """
    engine = get_engine()
    with engine.begin() as c:
        row = c.execute(select(s.users).where(s.users.c.google_sub == sub)).first()
        if row is None:
            row = c.execute(
                select(s.users).where(s.users.c.email == email.strip().lower())
            ).first()

        if row is not None:
            c.execute(
                s.users.update().where(s.users.c.id == row._mapping["id"]).values(
                    google_sub=sub, email=email.strip().lower(),
                    display_name=name, avatar_url=picture, last_login_at=_now(),
                )
            )
            refreshed = c.execute(
                select(s.users).where(s.users.c.id == row._mapping["id"])
            ).first()
            return dict(refreshed._mapping)

        result = c.execute(
            s.users.insert().values(
                email=email.strip().lower(), google_sub=sub, display_name=name,
                avatar_url=picture, plan="free", last_login_at=_now(),
            )
        )
        created = c.execute(
            select(s.users).where(s.users.c.id == result.inserted_primary_key[0])
        ).first()
        return dict(created._mapping)


def user_by_email(email: str) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.users).where(s.users.c.email == email.strip().lower())
        ).first()
    return dict(row._mapping) if row else None


def user_by_id(user_id: int) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(select(s.users).where(s.users.c.id == user_id)).first()
    return dict(row._mapping) if row else None


# --------------------------------------------------------------------------- #
# subscription
# --------------------------------------------------------------------------- #


def extend_plan(user_id: int, plan: str, months: int) -> datetime:
    """Extend a subscription, stacking onto unused time rather than discarding it.

    Someone who renews early should not lose the days they already paid for, so
    the new period starts from the later of now and the current expiry.
    """
    with get_engine().begin() as c:
        row = c.execute(
            select(s.users.c.plan_expires_at).where(s.users.c.id == user_id)
        ).first()
        current = row[0] if row else None
        if isinstance(current, str):
            current = datetime.fromisoformat(current)
        if current is not None and current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)

        base = current if (current and current > _now()) else _now()
        new_expiry = base + timedelta(days=31 * months)
        c.execute(
            s.users.update().where(s.users.c.id == user_id).values(
                plan=plan, plan_expires_at=new_expiry
            )
        )
    return new_expiry


def record_payment(
    user_id: int, payment_id: str, order_id: str, amount_paise: int,
    status: str, plan: str, months: int, raw: dict,
) -> bool:
    """Insert a payment. Returns False if it was already recorded.

    The browser redirect and the webhook both arrive for the same payment, so this
    has to be idempotent — otherwise a user gets two months for one charge.
    """
    with get_engine().begin() as c:
        result = c.execute(
            upsert_nothing(
                s.payments,
                {
                    "user_id": user_id, "provider": "razorpay",
                    "provider_payment_id": payment_id, "provider_order_id": order_id,
                    "amount_paise": amount_paise, "currency": "INR", "status": status,
                    "plan": plan, "months": months, "raw": raw, "created_at": _now(),
                },
                ["provider_payment_id"],
            )
        )
        return result.rowcount > 0


def list_payments(user_id: int, limit: int = 24) -> list[dict]:
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(
                    s.payments.c.provider_payment_id, s.payments.c.amount_paise,
                    s.payments.c.status, s.payments.c.plan, s.payments.c.months,
                    s.payments.c.created_at,
                )
                .where(s.payments.c.user_id == user_id)
                .order_by(desc(s.payments.c.id))
                .limit(limit)
            )
        )


# --------------------------------------------------------------------------- #
# sessions
# --------------------------------------------------------------------------- #


def create_session(token_hash: str, user_id: int, ttl_days: int = 30) -> None:
    with get_engine().begin() as c:
        c.execute(
            s.sessions.insert().values(
                token_hash=token_hash,
                user_id=user_id,
                expires_at=_now() + timedelta(days=ttl_days),
            )
        )


def session_user(token_hash: str) -> dict | None:
    """Resolve a session to its user, rejecting anything expired."""
    with get_engine().begin() as c:
        row = c.execute(
            select(s.users)
            .join(s.sessions, s.sessions.c.user_id == s.users.c.id)
            .where(
                and_(
                    s.sessions.c.token_hash == token_hash,
                    s.sessions.c.expires_at > _now(),
                    s.users.c.is_active.is_(True),
                )
            )
        ).first()
    return dict(row._mapping) if row else None


def delete_session(token_hash: str) -> None:
    with get_engine().begin() as c:
        c.execute(s.sessions.delete().where(s.sessions.c.token_hash == token_hash))


def purge_expired_sessions() -> int:
    with get_engine().begin() as c:
        return c.execute(s.sessions.delete().where(s.sessions.c.expires_at <= _now())).rowcount


# --------------------------------------------------------------------------- #
# documents, profile, targets
# --------------------------------------------------------------------------- #


def save_document(user_id: int, filename: str, content: str) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.documents,
                {"user_id": user_id, "filename": filename, "content": content,
                 "created_at": _now()},
                ["user_id", "filename"],
                ["content", "created_at"],
            )
        )


def list_documents(user_id: int) -> list[dict]:
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(s.documents.c.filename, s.documents.c.created_at,
                       func.length(s.documents.c.content).label("chars"))
                .where(s.documents.c.user_id == user_id)
                .order_by(s.documents.c.filename)
            )
        )


def document_bundle(user_id: int) -> str:
    """All of a user's documents concatenated, the shape Agent 1 expects."""
    with get_engine().begin() as c:
        rows = _rows(
            c.execute(
                select(s.documents.c.filename, s.documents.c.content)
                .where(s.documents.c.user_id == user_id)
                .order_by(s.documents.c.filename)
            )
        )
    return "\n\n".join(f"===== FILE: {r['filename']} =====\n{r['content']}" for r in rows)


def delete_document(user_id: int, filename: str) -> None:
    with get_engine().begin() as c:
        c.execute(
            s.documents.delete().where(
                and_(s.documents.c.user_id == user_id, s.documents.c.filename == filename)
            )
        )


def save_profile(user_id: int, graph: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.profiles,
                {"user_id": user_id, "graph": graph, "updated_at": _now()},
                ["user_id"],
                ["graph", "updated_at"],
            )
        )


def load_profile(user_id: int) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.profiles.c.graph).where(s.profiles.c.user_id == user_id)
        ).first()
    return row[0] if row else None


def save_targets(user_id: int, config: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.targets,
                {"user_id": user_id, "config": config, "updated_at": _now()},
                ["user_id"],
                ["config", "updated_at"],
            )
        )


def load_targets(user_id: int) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(select(s.targets.c.config).where(s.targets.c.user_id == user_id)).first()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# shared: job postings and company intel
# --------------------------------------------------------------------------- #


def upsert_postings(postings: list[dict]) -> int:
    """Shared across tenants — one fetch serves every user."""
    if not postings:
        return 0
    engine = get_engine()
    with engine.begin() as c:
        before = c.execute(select(func.count()).select_from(s.job_postings)).scalar_one()
        for p in postings:
            c.execute(
                upsert(
                    s.job_postings,
                    {**p, "fetched_at": _now()},
                    ["external_id"],
                    ["title", "description", "location", "url", "fetched_at"],
                )
            )
        after = c.execute(select(func.count()).select_from(s.job_postings)).scalar_one()
    return int(after - before)


def posting(external_id: str) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.job_postings).where(s.job_postings.c.external_id == external_id)
        ).first()
    return dict(row._mapping) if row else None


def postings_count() -> int:
    with get_engine().begin() as c:
        return int(c.execute(select(func.count()).select_from(s.job_postings)).scalar_one())


def unscored_postings(user_id: int, limit: int = 5000) -> list[dict]:
    """Postings this user has not scored yet. Other users' scores are irrelevant."""
    scored = select(s.fit_scores.c.external_id).where(s.fit_scores.c.user_id == user_id)
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(s.job_postings)
                .where(s.job_postings.c.external_id.not_in(scored))
                .limit(limit)
            )
        )


def save_company_intel(company: str, intel: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.company_intel,
                {"company": company, "intel": intel, "created_at": _now()},
                ["company"],
                ["intel", "created_at"],
            )
        )


def load_company_intel(company: str) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.company_intel.c.intel).where(s.company_intel.c.company == company)
        ).first()
    return row[0] if row else None


# --------------------------------------------------------------------------- #
# per-user: scores
# --------------------------------------------------------------------------- #


def save_fit_score(user_id: int, external_id: str, score: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.fit_scores,
                {
                    "user_id": user_id, "external_id": external_id,
                    "score": score["score"], "verdict": score["verdict"],
                    "matched_skills": score.get("matched_skills", []),
                    "missing_skills": score.get("missing_skills", []),
                    "reasoning": score.get("reasoning", ""), "scored_at": _now(),
                },
                ["user_id", "external_id"],
                ["score", "verdict", "matched_skills", "missing_skills", "reasoning", "scored_at"],
            )
        )


def scored_count(user_id: int) -> int:
    with get_engine().begin() as c:
        return int(
            c.execute(
                select(func.count()).select_from(s.fit_scores)
                .where(s.fit_scores.c.user_id == user_id)
            ).scalar_one()
        )


def top_matches(user_id: int, limit: int = 30, min_score: int = 0) -> list[dict]:
    j, f, a = s.job_postings, s.fit_scores, s.applications
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(
                    j.c.external_id, j.c.company, j.c.title, j.c.location, j.c.url,
                    f.c.score, f.c.verdict, f.c.matched_skills, f.c.missing_skills,
                    f.c.reasoning, a.c.stage,
                )
                .select_from(
                    f.join(j, j.c.external_id == f.c.external_id).outerjoin(
                        a, and_(a.c.external_id == f.c.external_id, a.c.user_id == user_id)
                    )
                )
                .where(and_(f.c.user_id == user_id, f.c.score >= min_score))
                .order_by(desc(f.c.score))
                .limit(limit)
            )
        )


def unpursued_strong(user_id: int, min_score: int, limit: int) -> list[dict]:
    pursued = select(s.applications.c.external_id).where(s.applications.c.user_id == user_id)
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(s.fit_scores.c.external_id, s.fit_scores.c.score)
                .where(
                    and_(
                        s.fit_scores.c.user_id == user_id,
                        s.fit_scores.c.score >= min_score,
                        s.fit_scores.c.external_id.not_in(pursued),
                    )
                )
                .order_by(desc(s.fit_scores.c.score))
                .limit(limit)
            )
        )


def unpursued_strong_count(user_id: int, min_score: int = 70) -> int:
    return len(unpursued_strong(user_id, min_score, 1000))


# --------------------------------------------------------------------------- #
# per-user: pipeline
# --------------------------------------------------------------------------- #


def add_application(user_id: int, external_id: str, stage: str = "shortlisted") -> bool:
    with get_engine().begin() as c:
        result = c.execute(
            upsert_nothing(
                s.applications,
                {"user_id": user_id, "external_id": external_id, "stage": stage,
                 "stage_entered_at": _now(), "created_at": _now()},
                ["user_id", "external_id"],
            )
        )
        return result.rowcount > 0


def set_stage(user_id: int, external_id: str, stage: str, note: str = "") -> None:
    with get_engine().begin() as c:
        current = c.execute(
            select(s.applications.c.notes).where(
                and_(s.applications.c.user_id == user_id,
                     s.applications.c.external_id == external_id)
            )
        ).first()
        notes = (current[0] or "") if current else ""
        if note:
            notes = f"{notes}{note}\n"
        c.execute(
            s.applications.update()
            .where(and_(s.applications.c.user_id == user_id,
                        s.applications.c.external_id == external_id))
            .values(stage=stage, stage_entered_at=_now(), notes=notes)
        )


def get_application(user_id: int, external_id: str) -> dict | None:
    j, a, f = s.job_postings, s.applications, s.fit_scores
    with get_engine().begin() as c:
        row = c.execute(
            select(
                a.c.stage, a.c.stage_entered_at, a.c.notes, a.c.external_id,
                j.c.company, j.c.title, j.c.url, j.c.location, j.c.description, f.c.score,
            )
            .select_from(
                a.join(j, j.c.external_id == a.c.external_id).outerjoin(
                    f, and_(f.c.external_id == a.c.external_id, f.c.user_id == user_id)
                )
            )
            .where(and_(a.c.user_id == user_id, a.c.external_id == external_id))
        ).first()
    return dict(row._mapping) if row else None


def list_applications(user_id: int, stage: str | None = None) -> list[dict]:
    j, a, f = s.job_postings, s.applications, s.fit_scores
    where = [a.c.user_id == user_id]
    if stage:
        where.append(a.c.stage == stage)
    with get_engine().begin() as c:
        rows = _rows(
            c.execute(
                select(
                    a.c.external_id, a.c.stage, a.c.stage_entered_at,
                    j.c.company, j.c.title, j.c.url, j.c.location, f.c.score,
                )
                .select_from(
                    a.join(j, j.c.external_id == a.c.external_id).outerjoin(
                        f, and_(f.c.external_id == a.c.external_id, f.c.user_id == user_id)
                    )
                )
                .where(and_(*where))
                .order_by(desc(f.c.score))
            )
        )
    # Compute age in Python: date arithmetic differs between Postgres and SQLite,
    # and the row count here is small enough that it does not matter.
    now = _now()
    for r in rows:
        entered = r["stage_entered_at"]
        if isinstance(entered, str):
            entered = datetime.fromisoformat(entered)
        if entered and entered.tzinfo is None:
            entered = entered.replace(tzinfo=timezone.utc)
        r["days_in_stage"] = int((now - entered).total_seconds() // 86400) if entered else 0
    return rows


def stage_counts(user_id: int) -> dict[str, int]:
    with get_engine().begin() as c:
        rows = c.execute(
            select(s.applications.c.stage, func.count().label("c"))
            .where(s.applications.c.user_id == user_id)
            .group_by(s.applications.c.stage)
        ).all()
    return {r[0]: r[1] for r in rows}


# --------------------------------------------------------------------------- #
# per-user: artifacts and reports
# --------------------------------------------------------------------------- #


def save_artifact(user_id: int, kind: str, payload: dict | list,
                  external_id: str | None = None) -> int:
    with get_engine().begin() as c:
        result = c.execute(
            s.artifacts.insert().values(
                user_id=user_id, external_id=external_id, kind=kind,
                payload=payload, created_at=_now(),
            )
        )
        return int(result.inserted_primary_key[0])


def latest_artifact(user_id: int, kind: str, external_id: str | None = None) -> Any:
    where = [s.artifacts.c.user_id == user_id, s.artifacts.c.kind == kind]
    if external_id is not None:
        where.append(s.artifacts.c.external_id == external_id)
    with get_engine().begin() as c:
        row = c.execute(
            select(s.artifacts.c.payload)
            .where(and_(*where))
            .order_by(desc(s.artifacts.c.id))
            .limit(1)
        ).first()
    return row[0] if row else None


def has_artifact(user_id: int, kind: str, external_id: str) -> bool:
    return latest_artifact(user_id, kind, external_id) is not None


def save_gap_report(user_id: int, target_role: str, report: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            s.gap_reports.insert().values(
                user_id=user_id, target_role=target_role, report=report, created_at=_now()
            )
        )


def latest_gap_report(user_id: int) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.gap_reports.c.report)
            .where(s.gap_reports.c.user_id == user_id)
            .order_by(desc(s.gap_reports.c.id))
            .limit(1)
        ).first()
    return row[0] if row else None


def save_comp_estimate(user_id: int, external_id: str, estimate: dict) -> None:
    with get_engine().begin() as c:
        c.execute(
            upsert(
                s.comp_estimates,
                {"user_id": user_id, "external_id": external_id, "estimate": estimate,
                 "created_at": _now()},
                ["user_id", "external_id"],
                ["estimate", "created_at"],
            )
        )


def load_comp_estimate(user_id: int, external_id: str) -> dict | None:
    with get_engine().begin() as c:
        row = c.execute(
            select(s.comp_estimates.c.estimate).where(
                and_(s.comp_estimates.c.user_id == user_id,
                     s.comp_estimates.c.external_id == external_id)
            )
        ).first()
    return row[0] if row else None


def save_mock_session(user_id: int, kind: str, transcript: list, avg_score: float,
                      external_id: str | None = None) -> None:
    with get_engine().begin() as c:
        c.execute(
            s.mock_sessions.insert().values(
                user_id=user_id, external_id=external_id, kind=kind,
                transcript=transcript, avg_score=avg_score, created_at=_now(),
            )
        )


def record_outcome(user_id: int, event: str, detail: str = "",
                   external_id: str | None = None) -> None:
    with get_engine().begin() as c:
        c.execute(
            s.outcomes.insert().values(
                user_id=user_id, external_id=external_id, event=event,
                detail=detail, occurred_at=_now(),
            )
        )


# --------------------------------------------------------------------------- #
# per-user: spend
# --------------------------------------------------------------------------- #


def log_call(user_id: int, **kw: Any) -> None:
    with get_engine().begin() as c:
        c.execute(s.llm_calls.insert().values(user_id=user_id, called_at=_now(), **kw))


def total_spend(user_id: int) -> float:
    with get_engine().begin() as c:
        value = c.execute(
            select(func.coalesce(func.sum(s.llm_calls.c.cost_usd), 0.0))
            .where(s.llm_calls.c.user_id == user_id)
        ).scalar_one()
    return float(value)


def spend_by_agent(user_id: int) -> list[dict]:
    with get_engine().begin() as c:
        return _rows(
            c.execute(
                select(
                    s.llm_calls.c.agent, s.llm_calls.c.model,
                    func.count().label("calls"),
                    func.sum(s.llm_calls.c.input_tokens).label("tin"),
                    func.sum(s.llm_calls.c.output_tokens).label("tout"),
                    func.sum(s.llm_calls.c.cache_read).label("cread"),
                    func.sum(s.llm_calls.c.cost_usd).label("cost"),
                )
                .where(s.llm_calls.c.user_id == user_id)
                .group_by(s.llm_calls.c.agent, s.llm_calls.c.model)
                .order_by(desc("cost"))
            )
        )



# --------------------------------------------------------------------------- #
# per-user target boards
# --------------------------------------------------------------------------- #
#
# Stored inside the user's targets config rather than in its own table: it is one
# more thing the user is targeting, and it keeps the shape simple.
#
# Note that discovered postings remain shared. One user adding a company makes its
# postings available to everyone, which is the intended behaviour — it is why
# discovery cost stays flat as the user base grows.


def load_boards(user_id: int) -> dict | None:
    config = load_targets(user_id) or {}
    boards = config.get("companies")
    return boards if boards else None


def save_boards(user_id: int, boards: dict) -> None:
    config = load_targets(user_id) or {}
    config["companies"] = boards
    save_targets(user_id, config)
