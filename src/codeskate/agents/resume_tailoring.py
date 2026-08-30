"""Agent 8 — Resume Tailoring.

Rewrites the resume for one specific job. It may reorder, reframe and re-word
what already exists. It may not add anything new.

The prompt says so, but prompts drift and models improvise, so the output is also
checked in code: every bullet must trace back to a supplied achievement, and
every skill claimed must be evidence-backed in the skill graph. Violations are
returned to the caller rather than silently accepted.

This is the highest-risk agent in the system. One fabricated line that survives
to a background check ends the product.
"""

from __future__ import annotations

import re

from ..llm import LLM
from ..models import SkillGraph, TailoredResume

SYSTEM = """You are an expert resume writer tailoring ONE candidate's resume to ONE job.

ABSOLUTE CONSTRAINT — you are working with a fixed set of facts:
* Every bullet you write MUST be a rewrite of one of the ACHIEVEMENTS supplied below.
* Set `source_achievement` to that achievement's exact headline.
* Use each achievement AT MOST ONCE. Never write two bullets from the same
  achievement — a duplicated accomplishment reads as padding and wastes a line.
* You may re-word, re-emphasise, reorder, and change which detail leads.
* You may NOT invent projects, employers, metrics, technologies or scope.
* You may NOT compute new figures. If the source says "800ms to 120ms", do not
  write "85% faster"; if it says "4% to 1.6%", do not write "2.4 percentage
  points". Derived numbers cannot be verified against the source and will be
  rejected. Quote the original figures.
* You may NOT upgrade a skill's depth. A skill at level 2 was used in coursework
  or a side project — list it if relevant, but never phrase it as production
  ownership, and never lead a bullet with it.
* Only list skills in `skills_line` that appear in the CLAIMABLE SKILLS list.
  Order them by level, strongest first.
* If the job wants something the candidate lacks, leave it out and note it in
  `omitted_and_why`. Do not paper over it.

Within those limits, be aggressive:
* Mirror the job's own vocabulary where it truthfully describes their work.
* Lead with the achievements closest to this role's core responsibility.
* Keep the metric in every bullet that has one — numbers are why bullets work.
* 5-8 bullets. Cut anything irrelevant to this job.

`fabrication_check`: state which achievement each bullet maps to, and confirm no
new facts were introduced."""


def _norm(s: str) -> set[str]:
    return {w for w in re.sub(r"[^a-z0-9 ]", " ", s.lower()).split() if len(w) > 2}


def _similar(a: str, b: str) -> float:
    ta, tb = _norm(a), _norm(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def run(llm: LLM, graph: SkillGraph, job_title: str, company: str, jd: str) -> TailoredResume:
    claimable = graph.claimable_skills()
    skills_block = "\n".join(
        f"- {s.name} (level {s.level}/5, {s.years:g}y)" for s in claimable
    ) or "- none"

    ach_block = "\n".join(
        f"- HEADLINE: {a.headline}\n"
        f"  metric: {a.metric or 'none stated'}\n"
        f"  skills: {', '.join(a.skills_used) or 'n/a'}\n"
        f"  source: {a.source}"
        for a in graph.achievements
    ) or "- none"

    user = (
        f"=== TARGET JOB ===\n{job_title} at {company}\n\n{jd[:5000]}\n\n"
        f"=== CLAIMABLE SKILLS (evidence-backed only) ===\n{skills_block}\n\n"
        f"=== ACHIEVEMENTS (your only source of facts) ===\n{ach_block}"
    )

    return llm.json_call(
        agent="resume_tailoring",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=TailoredResume,
        max_tokens=4000,
    )


def verify(resume: TailoredResume, graph: SkillGraph, threshold: float = 0.34) -> list[str]:
    """Programmatic fabrication check. Returns human-readable violations.

    Deliberately independent of the prompt — if the model stops obeying, this
    still catches it.
    """
    violations: list[str] = []

    headlines = [a.headline for a in graph.achievements]
    for i, bullet in enumerate(resume.bullets, 1):
        if not headlines:
            violations.append(f"bullet {i}: no achievements exist to source from")
            continue
        best = max(headlines, key=lambda h: _similar(bullet.source_achievement, h))
        if _similar(bullet.source_achievement, best) < threshold:
            violations.append(
                f"bullet {i}: source_achievement {bullet.source_achievement!r} "
                f"does not match any supplied achievement"
            )

    claimable = {s.name.lower() for s in graph.claimable_skills()}
    for skill in resume.skills_line:
        if skill.lower() not in claimable:
            # Tolerate trivial formatting differences, reject genuine additions.
            if not any(_similar(skill, c) >= 0.6 for c in claimable):
                violations.append(f"skills_line: {skill!r} is not an evidence-backed skill")

    # Numbers appearing in bullets should exist somewhere in the source facts.
    source_text = " ".join(
        f"{a.headline} {a.metric or ''}" for a in graph.achievements
    )
    source_nums = set(re.findall(r"\d+(?:\.\d+)?", source_text))
    for i, bullet in enumerate(resume.bullets, 1):
        for num in re.findall(r"\d+(?:\.\d+)?", bullet.text):
            if num not in source_nums and len(num) > 1:
                violations.append(
                    f"bullet {i}: number {num!r} does not appear in any source achievement"
                )

    return violations


def to_markdown(resume: TailoredResume, job_title: str, company: str) -> str:
    lines = [
        f"# {resume.headline}",
        f"\n*Tailored for {job_title} at {company}*\n",
        "## Summary",
        resume.summary,
        "\n## Experience",
    ]
    for b in resume.bullets:
        lines.append(f"- {b.text}")
        lines.append(f"  <!-- from: {b.source_achievement} -->")
    lines += ["\n## Skills", ", ".join(resume.skills_line)]
    if resume.omitted_and_why:
        lines += ["\n## Deliberately omitted", *(f"- {o}" for o in resume.omitted_and_why)]
    return "\n".join(lines)
