"""Owner-facing metrics.

What this deliberately does NOT expose: uploaded documents, skill graphs,
tailored resumes, or decrypted API keys. The owner needs to know how many people
signed up, where they stall, and what is breaking — none of which requires reading
anyone's resume.

That boundary is worth keeping even though the owner controls the database. It
limits the damage of a compromised admin account, and it makes "we do not read
your documents" a claim that is actually true rather than a policy promise.

Access is by email allowlist in ADMIN_EMAILS. Unset means nobody is an admin,
which is the correct default: a misconfigured deployment should lock the owner
out, not open the panel to everyone.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, desc, func, select

from . import schema as s
from .engine import get_engine


def admin_emails() -> set[str]:
    raw = os.getenv("ADMIN_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_admin(email: str) -> bool:
    allowed = admin_emails()
    return bool(allowed) and email.strip().lower() in allowed


def _now() -> datetime:
    return datetime.now(timezone.utc)


def overview() -> dict:
    """Counts and funnel. One round trip per metric, all aggregates."""
    engine = get_engine()
    week_ago = _now() - timedelta(days=7)
    day_ago = _now() - timedelta(days=1)

    with engine.begin() as c:
        total_users = c.execute(select(func.count()).select_from(s.users)).scalar_one()
        new_week = c.execute(
            select(func.count()).select_from(s.users).where(s.users.c.created_at >= week_ago)
        ).scalar_one()
        active_day = c.execute(
            select(func.count()).select_from(s.users)
            .where(s.users.c.last_login_at >= day_ago)
        ).scalar_one()
        disabled = c.execute(
            select(func.count()).select_from(s.users).where(s.users.c.is_active.is_(False))
        ).scalar_one()

        # Activation funnel. Each step is where users stop, which is the only
        # number that tells you what to fix next.
        with_key = c.execute(select(func.count()).select_from(s.user_keys)).scalar_one()
        with_docs = c.execute(
            select(func.count(func.distinct(s.documents.c.user_id)))
        ).scalar_one()
        with_profile = c.execute(select(func.count()).select_from(s.profiles)).scalar_one()
        with_scores = c.execute(
            select(func.count(func.distinct(s.fit_scores.c.user_id)))
        ).scalar_one()
        with_apps = c.execute(
            select(func.count(func.distinct(s.applications.c.user_id)))
        ).scalar_one()
        with_applied = c.execute(
            select(func.count(func.distinct(s.applications.c.user_id)))
            .where(s.applications.c.stage.not_in(["shortlisted", "tailored"]))
        ).scalar_one()

        postings = c.execute(select(func.count()).select_from(s.job_postings)).scalar_one()
        intel_cached = c.execute(select(func.count()).select_from(s.company_intel)).scalar_one()

        calls = c.execute(select(func.count()).select_from(s.llm_calls)).scalar_one()
        # Spend is on users' own keys. Useful as a usage signal, not as a bill.
        spend = c.execute(
            select(func.coalesce(func.sum(s.llm_calls.c.cost_usd), 0.0))
        ).scalar_one()

        queue_rows = c.execute(
            select(s.queue_jobs.c.state, func.count().label("n")).group_by(s.queue_jobs.c.state)
        ).all()

        recent_errors = c.execute(
            select(
                s.queue_jobs.c.id, s.queue_jobs.c.kind, s.queue_jobs.c.error,
                s.queue_jobs.c.updated_at, s.queue_jobs.c.attempts,
            )
            .where(s.queue_jobs.c.state == "error")
            .order_by(desc(s.queue_jobs.c.id))
            .limit(15)
        ).all()

        signups = c.execute(
            select(
                func.date(s.users.c.created_at).label("day"), func.count().label("n")
            ).group_by("day").order_by(desc("day")).limit(14)
        ).all()

    def step(label: str, n: int) -> dict:
        return {
            "step": label, "users": int(n),
            "pct": round(n / total_users * 100, 1) if total_users else 0.0,
        }

    return {
        "users": {
            "total": int(total_users), "new_7d": int(new_week),
            "active_24h": int(active_day), "disabled": int(disabled),
        },
        "funnel": [
            step("signed up", total_users),
            step("added API key", with_key),
            step("uploaded documents", with_docs),
            step("built skill graph", with_profile),
            step("scored matches", with_scores),
            step("shortlisted a job", with_apps),
            step("actually applied", with_applied),
        ],
        "shared": {"postings": int(postings), "company_intel_cached": int(intel_cached)},
        "usage": {"llm_calls": int(calls), "spend_on_user_keys_usd": round(float(spend), 4)},
        "queue": {r[0]: int(r[1]) for r in queue_rows},
        "recent_errors": [
            {"job_id": r[0], "kind": r[1], "error": (r[2] or "")[:200],
             "at": str(r[3]), "attempts": r[4]}
            for r in recent_errors
        ],
        "signups_by_day": [{"day": str(r[0]), "count": int(r[1])} for r in signups],
    }


def users(limit: int = 100, offset: int = 0) -> dict:
    """Per-user progress. Status only — no documents, no graphs, no key material."""
    engine = get_engine()

    with engine.begin() as c:
        total = c.execute(select(func.count()).select_from(s.users)).scalar_one()
        rows = c.execute(
            select(
                s.users.c.id, s.users.c.email, s.users.c.created_at,
                s.users.c.last_login_at, s.users.c.is_active, s.users.c.spend_limit_usd,
                s.user_keys.c.provider, s.user_keys.c.key_hint,
                select(func.count()).select_from(s.documents)
                .where(s.documents.c.user_id == s.users.c.id)
                .scalar_subquery().label("docs"),
                select(func.count()).select_from(s.profiles)
                .where(s.profiles.c.user_id == s.users.c.id)
                .scalar_subquery().label("has_profile"),
                select(func.count()).select_from(s.fit_scores)
                .where(s.fit_scores.c.user_id == s.users.c.id)
                .scalar_subquery().label("scored"),
                select(func.count()).select_from(s.applications)
                .where(s.applications.c.user_id == s.users.c.id)
                .scalar_subquery().label("pipeline"),
                select(func.coalesce(func.sum(s.llm_calls.c.cost_usd), 0.0))
                .where(s.llm_calls.c.user_id == s.users.c.id)
                .scalar_subquery().label("spend"),
            )
            .select_from(s.users.outerjoin(s.user_keys, s.user_keys.c.user_id == s.users.c.id))
            .order_by(desc(s.users.c.id))
            .limit(limit).offset(offset)
        ).all()

    return {
        "total": int(total),
        "users": [
            {
                "id": r.id, "email": r.email, "created_at": str(r.created_at),
                "last_login_at": str(r.last_login_at) if r.last_login_at else None,
                "is_active": bool(r.is_active), "spend_limit_usd": float(r.spend_limit_usd),
                # Provider and last four characters only. The key itself is never
                # decrypted here.
                "key_provider": r.provider, "key_hint": r.key_hint,
                "documents": int(r.docs), "has_profile": bool(r.has_profile),
                "scored": int(r.scored), "pipeline": int(r.pipeline),
                "spend_usd": round(float(r.spend), 4),
            }
            for r in rows
        ],
    }


def set_active(user_id: int, active: bool) -> None:
    """Disable an account. Sessions are dropped so the block takes effect at once."""
    with get_engine().begin() as c:
        c.execute(s.users.update().where(s.users.c.id == user_id).values(is_active=active))
        if not active:
            c.execute(s.sessions.delete().where(s.sessions.c.user_id == user_id))
