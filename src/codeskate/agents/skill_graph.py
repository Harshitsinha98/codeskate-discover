"""Agent 2 — Skill Graph.

Turns raw resume text into a structured, evidence-backed skill graph.

The hard rule, enforced in the prompt and again in the schema: a skill only
exists if there is evidence for it. This is not a quality nicety — it is what
stops the downstream tailoring agent from inventing experience and getting the
candidate caught in a background check.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import SkillGraph

SYSTEM = """You are a technical recruiter building a rigorous skill inventory from a \
candidate's own documents.

Rules you must not break:
1. Extract ONLY what the documents support. Never infer, upgrade or invent a skill.
2. Every skill needs at least one evidence entry naming the role/project it came from.
   If a skill is merely listed in a "Skills:" section with no supporting work
   anywhere, record it with level 1 and an empty evidence list.
3. Levels: 1=mentioned only, 2=used in coursework/side project, 3=shipped in
   production, 4=owned a significant production system, 5=recognised expert
   (talks, OSS ownership, deep specialisation).
4. Prefer the candidate's own numbers. Copy metrics verbatim where stated
   (latency, scale, revenue, users, %). Do not invent or round them.
5. Achievements should be reusable, self-contained accomplishment statements —
   these become the raw material for tailoring later.
6. Be conservative on total_years_experience: count professional work only,
   not education.
7. Fill in `years` for every skill by adding up the employment periods where it
   was actually used. If someone held a role from 2023-06 to now and that role
   used Python, Python has roughly that many years. Leaving `years` at 0 makes
   the profile useless to the scoring agent, which weighs recency and depth.
8. Set `last_used_year` from the role's end date, or the current year if ongoing."""


def build(llm: LLM, raw_text: str) -> SkillGraph:
    return llm.json_call(
        agent="skill_graph",
        tier="smart",  # quality-critical: worth the better model
        system=SYSTEM,
        user=f"Candidate documents:\n\n{raw_text}",
        schema=SkillGraph,
        max_tokens=8000,
        cache_system=True,
    )


def profile_brief(graph: SkillGraph, max_skills: int = 40) -> str:
    """Compact profile text reused as the cached prefix for every downstream call.

    The PROVEN/UNPROVEN marker is load-bearing. Without it, consuming agents
    cannot tell an evidenced skill from a self-claimed one, and the gap analysis
    agent ends up flagging well-evidenced skills as unverified.
    """
    skills = sorted(graph.skills, key=lambda s: (-s.level, s.name))[:max_skills]
    lines = [
        f"Seniority: {graph.seniority or 'unknown'}",
        f"Years of experience: {graph.total_years_experience}",
        f"Headline: {graph.headline or 'n/a'}",
        "",
        "SKILLS (name | level/5 | years | PROVEN means backed by a concrete achievement):",
    ]
    for s in skills:
        status = "PROVEN" if s.evidence else "UNPROVEN — self-claimed only"
        lines.append(f"- {s.name} | {s.level} | {s.years:g} | {status}")

    lines += ["", "KEY ACHIEVEMENTS (the evidence behind the PROVEN skills):"]
    lines += [
        f"- {a.headline}" + (f" [{a.metric}]" if a.metric else "")
        for a in graph.achievements[:15]
    ]
    return "\n".join(lines)
