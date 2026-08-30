"""Agent 12 — Interview Prep.

Builds a company- and role-specific brief: the questions likely to come up, STAR
stories assembled from the candidate's real achievements, and — the part most prep
tools avoid — where this candidate is going to get caught out.

The weak-spots section is the point. Anyone can list common questions.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import CompanyIntel, PrepBrief, SkillGraph

SYSTEM = """You are preparing one candidate for one specific interview.

Rules:
1. `likely_questions`: 8-12 questions, weighted to what this JD actually
   emphasises. For each, `why_asked` explains what the interviewer is really
   testing, and `answer_outline` is a skeleton — bullet logic, not a script to
   memorise.
2. `star_stories`: build 4-6 from the candidate's supplied achievements ONLY. Do
   not invent situations, and do not embellish results. Keep their numbers exact.
   `use_for` lists which of your questions each story answers.
3. `weak_spots`: be blunt and specific. Where will the interviewer press and find
   nothing? Which claimed skill has thin evidence? Which gap in the JD will come
   up? This section is more valuable than the question list — do not soften it.
4. `questions_to_ask_them`: questions that reveal how the team actually works.
   Nothing that could be answered from the careers page.
5. `role_focus`: the 3-4 areas this loop will actually be decided on."""


def run(
    llm: LLM,
    graph: SkillGraph,
    job_title: str,
    company: str,
    jd: str,
    intel: CompanyIntel | None = None,
    stage: str = "screen",
) -> PrepBrief:
    from .skill_graph import profile_brief

    ach = "\n".join(
        f"- {a.headline} | metric: {a.metric or 'none'} | skills: {', '.join(a.skills_used)}"
        for a in graph.achievements
    ) or "- none recorded"

    intel_block = ""
    if intel:
        intel_block = (
            f"\n=== COMPANY BRIEFING (confidence: {intel.confidence}) ===\n"
            f"{intel.what_they_do}\n"
            f"Likely process: {'; '.join(intel.likely_interview_process) or 'unknown'}\n"
            f"Stack: {', '.join(intel.likely_tech_stack) or 'unknown'}\n"
        )

    user = (
        f"INTERVIEW STAGE: {stage}\nROLE: {job_title} at {company}\n\n"
        f"=== JOB DESCRIPTION ===\n{jd[:4000]}\n"
        f"{intel_block}\n"
        f"=== CANDIDATE PROFILE ===\n{profile_brief(graph)}\n\n"
        f"=== ACHIEVEMENTS (only source for STAR stories) ===\n{ach}"
    )
    return llm.json_call(
        agent="interview_prep",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=PrepBrief,
        max_tokens=8000,
    )


def to_markdown(brief: PrepBrief, job_title: str, company: str) -> str:
    lines = [
        f"# Interview prep — {job_title} @ {company}",
        "\n## This loop will be decided on",
        *(f"- {f}" for f in brief.role_focus),
        "\n## Where you will get caught out",
        "*Read this section twice. It is the one that changes the outcome.*\n",
        *(f"- {w}" for w in brief.weak_spots),
        "\n## Likely questions",
    ]
    for q in brief.likely_questions:
        lines += [
            f"\n### [{q.kind}] {q.question}",
            f"*Really testing:* {q.why_asked}\n",
            q.answer_outline,
        ]
    lines += ["\n## Your STAR stories"]
    for s in brief.star_stories:
        lines += [
            f"\n### {s.title}",
            f"- **S:** {s.situation}",
            f"- **T:** {s.task}",
            f"- **A:** {s.action}",
            f"- **R:** {s.result}",
            f"- *Use for:* {', '.join(s.use_for)}",
        ]
    lines += ["\n## Ask them", *(f"- {q}" for q in brief.questions_to_ask_them)]
    return "\n".join(lines)
