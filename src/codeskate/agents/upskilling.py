"""Agent 15 — Upskilling.

Turns the gap report into a dated plan whose milestones produce *artefacts*, not
completed courses.

The design constraint that makes this useful: every milestone must end in
something that becomes legitimate evidence in the skill graph. That closes the
loop — finish the plan, add the proof to your brag document, re-run `profile`, and
the tailoring agent can now truthfully claim the skill. A certificate does not
achieve that; a shipped project does.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import GapReport, SkillGraph, UpskillPlan

SYSTEM = """You are building a short, brutally practical upskilling plan.

Rules:
1. Address blocking gaps first. Ignore nice-to-have gaps entirely unless there is
   spare time in the plan — a focused 4 weeks beats a scattered 12.
2. Every milestone's `deliverable` must be a concrete artefact: a deployed service,
   a migration, a benchmark with numbers, a merged open-source PR, a load test with
   results. Never "complete a course", "read the docs", or "practise".
3. `proof` is what an interviewer could be shown or told — a repo, a metric, a
   write-up. This is what makes the skill claimable afterwards.
4. Scope for someone with a job and limited evenings. One meaningful deliverable
   per week is realistic; three is a fantasy and the plan will be abandoned.
5. `resume_lines_unlocked` are bullets that become TRUTHFUL once the plan is done.
   Write them in the same voice as a real resume bullet, with the metric the
   deliverable will produce.
6. Keep the plan between 2 and 8 weeks. If the gaps genuinely need longer, say so
   in the milestones rather than stretching the timeline vaguely."""


def run(
    llm: LLM, graph: SkillGraph, gap: GapReport, weeks: int = 6, hours_per_week: int = 8
) -> UpskillPlan:
    gaps_block = "\n".join(
        f"- [{g.severity}] {g.skill}: {g.why_it_matters}\n  suggested proof: {g.fastest_proof}"
        for g in gap.gaps
    ) or "- none recorded"

    claimable = ", ".join(s.name for s in graph.claimable_skills()) or "none"

    user = (
        f"TARGET ROLE: {gap.target_role}\n"
        f"CURRENT READINESS: {gap.readiness_pct}%\n"
        f"TIME AVAILABLE: {weeks} weeks at ~{hours_per_week} hours/week\n\n"
        f"=== GAPS TO CLOSE ===\n{gaps_block}\n\n"
        f"=== SKILLS THEY ALREADY HAVE EVIDENCE FOR (build on these) ===\n{claimable}"
    )
    return llm.json_call(
        agent="upskilling",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=UpskillPlan,
        max_tokens=4000,
    )


def to_markdown(plan: UpskillPlan) -> str:
    lines = [
        f"# {plan.weeks}-week plan — {plan.target_role}",
        "\nEvery milestone ends in an artefact. When you finish one, add it to your "
        "brag document and re-run `codeskate profile` — the skill becomes claimable.\n",
    ]
    for m in sorted(plan.milestones, key=lambda m: m.week):
        lines += [
            f"\n## Week {m.week} — {m.focus}",
            f"- **Deliverable:** {m.deliverable}",
            f"- **Proof:** {m.proof}",
        ]
    if plan.resume_lines_unlocked:
        lines += [
            "\n## Resume lines this unlocks",
            "*Not claimable yet. These become true when the deliverables exist.*\n",
            *(f"- {line}" for line in plan.resume_lines_unlocked),
        ]
    return "\n".join(lines)
