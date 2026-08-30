"""Agent 16 — Career Manager (the orchestrator).

The only agent a user should have to talk to. It does not do work itself; it
looks at the whole system state and answers one question: *what should I do next?*

The action engine is pure rules — free, deterministic, instant. The LLM is used
only for an optional short brief on top, because "you have 3 stale applications"
does not need a language model to work out.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from pydantic import BaseModel

from .. import db
from ..llm import LLM
from . import pipeline


class DailyBrief(BaseModel):
    brief: str


@dataclass
class Action:
    priority: int  # lower runs first
    label: str
    command: str
    why: str

    def __str__(self) -> str:
        return f"{self.label} -> {self.command}"


def next_actions(conn: sqlite3.Connection, limit: int = 8) -> list[Action]:
    """Inspect all state and return a prioritised to-do list. No LLM, no cost."""
    actions: list[Action] = []

    profile = db.load_profile(conn)
    job_count = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    unscored = conn.execute(
        """SELECT COUNT(*) AS c FROM jobs j
           LEFT JOIN fit_scores f ON f.external_id = j.external_id
           WHERE f.external_id IS NULL"""
    ).fetchone()["c"]
    counts = db.stage_counts(conn)

    # --- setup gates ---------------------------------------------------------
    if profile is None:
        return [
            Action(
                0,
                "Build your skill graph",
                "codeskate profile",
                "Nothing else can run without it. Put resume.pdf and brag.md in data/inbox/ first.",
            )
        ]

    if job_count == 0:
        actions.append(
            Action(1, "Pull live jobs", "codeskate discover", "No jobs in the database yet. Free.")
        )

    if db.latest_gap_report(conn) is None:
        actions.append(
            Action(
                2,
                "Run gap analysis",
                "codeskate gaps --role '<your target role>'",
                "Tells you what is actually blocking you before you spend applications finding out.",
            )
        )

    if unscored > 0 and job_count:
        actions.append(
            Action(
                3,
                f"Score {min(unscored, 40)} of {unscored} unscored jobs",
                "codeskate score --limit 40",
                "The prefilter drops most of these for free; only survivors cost anything.",
            )
        )

    # --- shortlisting --------------------------------------------------------
    strong_unpursued = conn.execute(
        """SELECT COUNT(*) AS c FROM fit_scores f
           LEFT JOIN applications a ON a.external_id = f.external_id
           WHERE f.score >= 70 AND a.external_id IS NULL"""
    ).fetchone()["c"]
    if strong_unpursued:
        actions.append(
            Action(
                4,
                f"Shortlist {strong_unpursued} strong match(es)",
                "codeskate shortlist --min-score 70",
                "Scored well but not yet in your pipeline.",
            )
        )

    # --- per-stage work ------------------------------------------------------
    for row in db.list_applications(conn, stage="shortlisted"):
        actions.append(
            Action(
                5,
                f"Tailor resume: {row['company']} — {row['title'][:40]}",
                f"codeskate tailor {row['external_id']}",
                "Shortlisted but no tailored resume yet.",
            )
        )

    for row in db.list_applications(conn, stage="tailored"):
        has_outreach = db.has_artifact(conn, "outreach", row["external_id"])
        if not has_outreach:
            actions.append(
                Action(
                    6,
                    f"Draft outreach: {row['company']}",
                    f"codeskate outreach {row['external_id']}",
                    "Resume is ready; cover letter and recruiter message are not.",
                )
            )
        else:
            actions.append(
                Action(
                    6,
                    f"Submit application: {row['company']}",
                    f"codeskate submit {row['external_id']}",
                    "Everything is drafted. Review the packet and apply.",
                )
            )

    # --- follow-ups ----------------------------------------------------------
    for row in pipeline.needs_followup(conn):
        actions.append(
            Action(
                7,
                f"Follow up ({row['days_in_stage']}d silent): {row['company']}",
                f"codeskate followup {row['external_id']}",
                f"In '{row['stage']}' for {row['days_in_stage']} days with no movement.",
            )
        )

    for row in pipeline.ghost_candidates(conn):
        actions.append(
            Action(
                9,
                f"Close out as ghosted: {row['company']}",
                f"codeskate stage {row['external_id']} ghosted",
                f"{row['days_in_stage']} days silent. Leaving it open distorts your funnel.",
            )
        )

    # --- interviews and offers ----------------------------------------------
    for stage in ("screen", "interview", "onsite"):
        for row in db.list_applications(conn, stage=stage):
            if not db.has_artifact(conn, "prep_brief", row["external_id"]):
                actions.append(
                    Action(
                        3,
                        f"Prep for {stage}: {row['company']}",
                        f"codeskate prep {row['external_id']}",
                        "Live interview stage with no prep brief. Highest-value action available.",
                    )
                )
            else:
                actions.append(
                    Action(
                        3,
                        f"Run a mock: {row['company']}",
                        f"codeskate mock {row['external_id']}",
                        "Brief exists — now rehearse against it.",
                    )
                )

    for row in db.list_applications(conn, stage="offer"):
        actions.append(
            Action(
                0,
                f"Negotiate offer: {row['company']}",
                f"codeskate negotiate {row['external_id']}",
                "Highest-leverage moment in the entire process. Do not accept before this.",
            )
        )

    # --- longer horizon ------------------------------------------------------
    gap = db.latest_gap_report(conn)
    if gap and any(g.get("severity") == "blocking" for g in gap.get("gaps", [])):
        actions.append(
            Action(
                8,
                "Build an upskilling plan",
                "codeskate upskill",
                "You have blocking gaps. Applications alone will not fix those.",
            )
        )

    applied_total = sum(counts.get(s, 0) for s in ("applied", "screen", "interview", "onsite", "offer", "rejected", "ghosted", "accepted"))
    if applied_total >= 10:
        actions.append(
            Action(
                8,
                "Review what is actually working",
                "codeskate learn",
                f"{applied_total} applications recorded — enough for a first signal.",
            )
        )

    actions.sort(key=lambda a: a.priority)
    return actions[:limit]


BRIEF_SYSTEM = """You are the candidate's career manager. You are given the current \
pipeline state and a rule-generated action list.

Write a short daily brief: 3-5 sentences maximum. Lead with the single most
important thing to do today and why. Be direct and specific — reference actual
companies and counts. No motivational filler, no restating the list back.

If the pipeline is thin, say so plainly rather than finding busy-work."""


def brief(llm: LLM, conn: sqlite3.Connection, actions: list[Action]) -> str:
    """Optional LLM summary. Cheap tier — this is not hard reasoning."""
    funnel = pipeline.funnel(conn)
    rates = pipeline.conversion_rates(conn)
    active = {k: v for k, v in funnel.items() if v}

    user = (
        f"PIPELINE: {active or 'empty'}\n"
        f"RATES: {rates}\n\n"
        "ACTION LIST:\n"
        + "\n".join(f"{i}. {a.label} ({a.why})" for i, a in enumerate(actions, 1))
    )

    return llm.json_call(
        agent="career_manager",
        tier="cheap",
        system=BRIEF_SYSTEM,
        user=user,
        schema=DailyBrief,
        max_tokens=600,
    ).brief
