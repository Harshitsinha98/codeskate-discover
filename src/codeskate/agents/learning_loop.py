"""Agent 18 — Learning Loop.

The compounding advantage, and the only agent that can tell you whether any of
the other seventeen are working.

Statistics are computed in SQL, deterministically and for free. The LLM only
interprets them. That split matters: a model asked to "analyse my job hunt" will
produce confident narrative from three data points, which is worse than useless
because it feels like insight.

The most important thing it measures is whether **fit score actually predicts
callbacks**. If high-scoring applications convert no better than low-scoring ones,
Agent 5 is not working and no amount of polish downstream will help.
"""

from __future__ import annotations

import sqlite3

from ..llm import LLM
from ..models import LearningInsight

# Below this many applications, any breakdown is noise dressed up as a finding.
MIN_SAMPLE = 10

RESPONDED = ("screen", "interview", "onsite", "offer", "accepted")

SYSTEM = """You are interpreting a candidate's job-hunt funnel data.

Rules:
1. Only make claims the numbers support. With fewer than ~30 applications per
   bucket, differences between buckets are noise — say that explicitly instead of
   reading a trend into them.
2. `confidence_warning` must state plainly what cannot be concluded at this sample
   size. Do not bury it or soften it. This is the most important field.
3. `recommended_changes` must be concrete and actionable against this system:
   a specific edit to config/targets.yaml, a specific prompt to revise, more
   companies in companies.yaml, more contacts in network.yaml. Not "apply to more
   jobs".
4. If fit score does not correlate with callbacks, say so directly — that means the
   scoring agent needs work, and it is the single most important finding available.
5. If referred applications outperform cold ones, quantify the gap and recommend
   shifting effort accordingly.
6. Never congratulate. Never speculate about causes the data cannot distinguish."""


def compute_stats(conn: sqlite3.Connection) -> dict:
    """All the arithmetic, in SQL. No LLM, no cost, no hallucination surface."""
    from . import pipeline

    responded_list = ",".join(f"'{s}'" for s in RESPONDED)

    total = conn.execute(
        f"""SELECT COUNT(*) AS c FROM applications
            WHERE stage IN ('applied','rejected','ghosted',{responded_list})"""
    ).fetchone()["c"]

    responded = conn.execute(
        f"SELECT COUNT(*) AS c FROM applications WHERE stage IN ({responded_list})"
    ).fetchone()["c"]

    # Does fit score predict a callback? The core validation of Agent 5.
    by_score = conn.execute(
        f"""SELECT CASE
                     WHEN f.score >= 85 THEN '85-100'
                     WHEN f.score >= 70 THEN '70-84'
                     WHEN f.score >= 55 THEN '55-69'
                     ELSE '0-54' END AS bucket,
                   COUNT(*) AS applications,
                   SUM(CASE WHEN a.stage IN ({responded_list}) THEN 1 ELSE 0 END) AS responses
            FROM applications a
            JOIN fit_scores f ON f.external_id = a.external_id
            WHERE a.stage NOT IN ('shortlisted','tailored')
            GROUP BY bucket ORDER BY bucket DESC"""
    ).fetchall()

    # Did a referral change the outcome?
    by_referral = conn.execute(
        f"""SELECT CASE WHEN EXISTS (
                     SELECT 1 FROM artifacts r
                     WHERE r.external_id = a.external_id AND r.kind = 'referral'
                   ) THEN 'referred' ELSE 'cold' END AS route,
                   COUNT(*) AS applications,
                   SUM(CASE WHEN a.stage IN ({responded_list}) THEN 1 ELSE 0 END) AS responses
            FROM applications a
            WHERE a.stage NOT IN ('shortlisted','tailored')
            GROUP BY route"""
    ).fetchall()

    by_company = conn.execute(
        f"""SELECT j.company, COUNT(*) AS applications,
                   SUM(CASE WHEN a.stage IN ({responded_list}) THEN 1 ELSE 0 END) AS responses
            FROM applications a JOIN jobs j ON j.external_id = a.external_id
            WHERE a.stage NOT IN ('shortlisted','tailored')
            GROUP BY j.company HAVING applications >= 2
            ORDER BY applications DESC LIMIT 10"""
    ).fetchall()

    def pct(rows: list[sqlite3.Row], key: str) -> list[dict]:
        out = []
        for r in rows:
            apps = r["applications"] or 0
            out.append(
                {
                    key: r[key],
                    "applications": apps,
                    "responses": r["responses"] or 0,
                    "response_rate_pct": round((r["responses"] or 0) / apps * 100, 1)
                    if apps
                    else None,
                }
            )
        return out

    mocks = conn.execute(
        "SELECT COUNT(*) AS c, AVG(avg_score) AS avg FROM mock_sessions"
    ).fetchone()

    return {
        "applications_sent": total,
        "responses": responded,
        "overall_response_rate_pct": round(responded / total * 100, 1) if total else None,
        "funnel": {k: v for k, v in pipeline.funnel(conn).items() if v},
        "by_fit_score": pct(by_score, "bucket"),
        "by_route": pct(by_referral, "route"),
        "by_company": pct(by_company, "company"),
        "mock_sessions": mocks["c"],
        "mock_avg_score": round(mocks["avg"], 1) if mocks["avg"] is not None else None,
        "sample_is_meaningful": total >= MIN_SAMPLE,
    }


def run(llm: LLM, stats: dict) -> LearningInsight:
    import json

    user = (
        f"FUNNEL DATA (computed, not estimated):\n{json.dumps(stats, indent=2)}\n\n"
        f"Note: a sample of {stats['applications_sent']} applications is "
        + ("workable for a first signal." if stats["sample_is_meaningful"] else "far too small for any conclusion.")
    )
    return llm.json_call(
        agent="learning_loop",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=LearningInsight,
        max_tokens=3000,
    )
