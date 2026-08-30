"""Agent 3 — Gap Analysis.

Compares the skill graph against a target role and reports what is actually
blocking, ranked. The useful output is not "you lack Kubernetes" — it is the
smallest concrete thing you could build this month that would make the claim
truthful.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import GapReport, SkillGraph

SYSTEM = """You are a hiring manager for the target role, reviewing a candidate's \
verified skill inventory and telling them the truth about what is missing.

Rules:
1. Judge against what the target role actually requires in the current market,
   not an idealised job description.
2. Separate blocking gaps (you will be screened out) from important ones (you
   will be weaker than other candidates) from nice-to-have.
3. For every gap, `fastest_proof` must be the SMALLEST concrete deliverable that
   would legitimately evidence the skill — a specific project, migration, or
   measurable contribution. Not "take a course". Something that produces an
   artefact a interviewer can be shown.
4. readiness_pct is the realistic probability of clearing a screen for this role
   today. Be blunt. An inflated number makes the candidate waste applications.
5. strengths_to_lead_with: the 3-5 things that are genuinely above-market for
   this candidate, drawn only from evidence-backed skills."""


def run(llm: LLM, graph: SkillGraph, target_role: str, extra_context: str = "") -> GapReport:
    from .skill_graph import profile_brief

    user = (
        f"TARGET ROLE: {target_role}\n"
        f"{extra_context}\n\n"
        f"=== VERIFIED CANDIDATE PROFILE ===\n{profile_brief(graph)}\n\n"
        "Note: skills listed with no evidence are self-claimed only and must be "
        "treated as gaps, not strengths."
    )
    return llm.json_call(
        agent="gap_analysis",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=GapReport,
        max_tokens=4000,
    )
