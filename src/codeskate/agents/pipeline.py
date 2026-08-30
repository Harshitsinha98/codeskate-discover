"""Agent 17 — Pipeline & Follow-up.

The state machine every other agent reads from. Entirely rule-based: no LLM, no
cost. Its job is to know where each application stands and what has gone stale.

Ghosting is the default outcome of a job hunt, so it is modelled explicitly
rather than left as "no news".
"""

from __future__ import annotations

import sqlite3

STAGES: list[str] = [
    "shortlisted",  # decided to pursue
    "tailored",     # resume + outreach generated
    "applied",      # submitted
    "screen",       # recruiter call scheduled/done
    "interview",    # technical round(s)
    "onsite",       # final loop
    "offer",
    "accepted",
    "rejected",
    "ghosted",
    "withdrawn",
]

TERMINAL = {"accepted", "rejected", "ghosted", "withdrawn"}
ACTIVE = [s for s in STAGES if s not in TERMINAL]

# Forward moves only, plus the ability to fail out of any active stage.
_FORWARD: dict[str, set[str]] = {
    "shortlisted": {"tailored", "withdrawn"},
    "tailored": {"applied", "withdrawn"},
    "applied": {"screen", "rejected", "ghosted", "withdrawn"},
    "screen": {"interview", "rejected", "ghosted", "withdrawn"},
    "interview": {"onsite", "offer", "rejected", "ghosted", "withdrawn"},
    "onsite": {"offer", "rejected", "ghosted", "withdrawn"},
    "offer": {"accepted", "rejected", "withdrawn"},
}

# Days of silence before the follow-up agent nudges you.
FOLLOWUP_AFTER: dict[str, int] = {
    "applied": 6,
    "screen": 5,
    "interview": 7,
    "onsite": 7,
    "offer": 3,
}

# Days of silence after which it is honestly a ghost, not a pending decision.
GHOST_AFTER: dict[str, int] = {
    "applied": 21,
    "screen": 18,
    "interview": 21,
    "onsite": 21,
}


class InvalidTransition(ValueError):
    pass


def can_move(current: str, target: str) -> bool:
    return target in _FORWARD.get(current, set())


def advance(conn: sqlite3.Connection, external_id: str, target: str, note: str = "") -> None:
    """Move an application forward, refusing illegal jumps.

    Guarding this matters: a silent bad transition corrupts the funnel metrics
    that the learning loop depends on.
    """
    from .. import db

    app = db.get_application(conn, external_id)
    if app is None:
        raise InvalidTransition(f"{external_id} is not in the pipeline — shortlist it first")
    if target not in STAGES:
        raise InvalidTransition(f"unknown stage {target!r}; valid: {', '.join(STAGES)}")

    current = app["stage"]
    if current == target:
        return
    if not can_move(current, target):
        allowed = ", ".join(sorted(_FORWARD.get(current, set()))) or "nothing (terminal stage)"
        raise InvalidTransition(f"cannot go {current} -> {target}. Allowed: {allowed}")

    db.set_stage(conn, external_id, target, note)
    db.record_outcome(conn, event=target, detail=note, external_id=external_id)


def needs_followup(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Applications that have gone quiet long enough to warrant a nudge."""
    from .. import db

    out = []
    for row in db.list_applications(conn):
        limit = FOLLOWUP_AFTER.get(row["stage"])
        ghost = GHOST_AFTER.get(row["stage"])
        days = row["days_in_stage"] or 0
        if limit is None or days < limit:
            continue
        if ghost is not None and days >= ghost:
            continue  # past nudging; it's a ghost
        out.append(row)
    return out


def ghost_candidates(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Silent long enough to close out. Keeping these 'open' fakes your funnel."""
    from .. import db

    return [
        row
        for row in db.list_applications(conn)
        if (limit := GHOST_AFTER.get(row["stage"])) is not None
        and (row["days_in_stage"] or 0) >= limit
    ]


def funnel(conn: sqlite3.Connection) -> dict[str, int]:
    """Counts per stage, including zeros, in pipeline order."""
    from .. import db

    counts = db.stage_counts(conn)
    return {stage: counts.get(stage, 0) for stage in STAGES}


def conversion_rates(conn: sqlite3.Connection) -> dict[str, float | None]:
    """Funnel rates. None where the sample is too small to mean anything."""
    from .. import db

    counts = db.stage_counts(conn)
    reached: dict[str, int] = {}
    for i, stage in enumerate(STAGES):
        if stage in TERMINAL:
            continue
        # Anyone in a later active stage also passed through this one.
        reached[stage] = sum(counts.get(s, 0) for s in STAGES[i:] if s not in TERMINAL) + sum(
            counts.get(s, 0) for s in ("accepted", "rejected", "ghosted") if i <= STAGES.index("applied")
        )

    applied = reached.get("applied", 0)
    screens = reached.get("screen", 0)
    offers = counts.get("offer", 0) + counts.get("accepted", 0)

    def rate(num: int, den: int) -> float | None:
        return round(num / den * 100, 1) if den >= 10 else None

    return {
        "callback_rate_pct": rate(screens, applied),
        "offer_rate_pct": rate(offers, applied),
        "applied_total": float(applied),
    }
