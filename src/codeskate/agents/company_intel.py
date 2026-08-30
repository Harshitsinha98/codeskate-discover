"""Agent 6 — Company Intel.

Builds a briefing for a company: what they do, how they probably interview, what
to talk about, and what the risks are.

Honest limitation: this runs on the job description plus the model's training
knowledge, so `confidence` is usually low-to-medium and anything time-sensitive
(funding, layoffs, headcount) may be stale. The schema forces the model to say so
rather than inventing a confident-sounding briefing.

The upgrade path is a public-web enrichment step — the company's own engineering
blog, careers page and docs — before this call. Doing that properly needs a fetch
layer with caching and rate limits, which is Phase 3.5 work.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import CompanyIntel

SYSTEM = """You are briefing a candidate before they interview at a company.

Rules:
1. Anchor everything you can in the job description supplied — that is the only
   source you have that is definitely current.
2. Where you rely on background knowledge, prefer durable facts (what the company
   sells, how the business makes money, well-established stack choices) over
   volatile ones (headcount, latest funding, current org structure).
3. `risks` must be real and specific: dependence on one revenue line, a crowded
   market, a stack choice that limits career growth, signs of churn in the JD's
   wording. If you have no substantiated risk, say so rather than padding.
4. `talking_points` must be things this candidate could credibly raise — an
   observation about their product or engineering problem, not trivia.
5. `questions_to_ask_them` should be questions that surface whether this is a good
   place to work, not questions that flatter the interviewer.
6. Set `confidence` honestly. "low" is the correct answer for a company you know
   little about beyond the posting. Never inflate it.
7. If you are unsure whether something is still true, leave it out."""


def run(
    llm: LLM, company: str, job_title: str, jd: str, extra_notes: str = ""
) -> CompanyIntel:
    user = (
        f"COMPANY: {company}\nROLE BEING BRIEFED FOR: {job_title}\n\n"
        f"=== JOB DESCRIPTION (current, trust this) ===\n{jd[:5000]}\n"
        + (f"\n=== NOTES SUPPLIED BY THE CANDIDATE ===\n{extra_notes}\n" if extra_notes else "")
    )
    return llm.json_call(
        agent="company_intel",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=CompanyIntel,
        max_tokens=3000,
    )


def to_markdown(intel: CompanyIntel) -> str:
    def block(title: str, items: list[str]) -> list[str]:
        return [f"\n## {title}", *(f"- {i}" for i in items)] if items else []

    lines = [
        f"# {intel.company}",
        f"\n*Confidence: {intel.confidence}* — verify anything time-sensitive yourself.\n",
        "## What they do",
        intel.what_they_do,
    ]
    if intel.business_model:
        lines += ["\n## Business model", intel.business_model]
    lines += block("Likely tech stack", intel.likely_tech_stack)
    lines += block("Likely interview process", intel.likely_interview_process)
    lines += block("Talking points", intel.talking_points)
    lines += block("Risks", intel.risks)
    lines += block("Questions to ask them", intel.questions_to_ask_them)
    return "\n".join(lines)
