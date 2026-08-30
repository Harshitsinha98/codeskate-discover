"""Agent 11 — Submission.

Assembles a review-ready application packet on disk and drafts answers to the
screening questions almost every ATS asks.

It stops short of clicking submit, and that is a design decision rather than an
unfinished feature:

* Auto-submitting needs your authenticated session on each job board. Driving that
  headlessly is exactly the behaviour those terms of service prohibit, and it is
  the fastest way to get an account restricted.
* Every ATS has a different form. Blind field-filling produces silently mangled
  applications, which is worse than not applying.
* One badly targeted auto-application costs you that company for a year.

So: the packet is generated for you, you review and submit, then `codeskate stage
<id> applied` records it. When this becomes a product, the honest version is
assisted submission inside the user's own browser session with explicit per-
application confirmation — not a background blaster.
"""

from __future__ import annotations

import re
import sqlite3

from pydantic import BaseModel, Field

from ..llm import LLM
from ..models import OutreachPack, SkillGraph, TailoredResume
from ..settings import OUT_DIR


class ScreeningAnswers(BaseModel):
    """The questions that block most ATS forms. Drafted once, reused."""

    why_this_company: str = Field(description="60-80 words, specific to them, no flattery")
    why_this_role: str = Field(description="60-80 words, tied to their actual work")
    biggest_relevant_project: str = Field(description="80-100 words, with the metric")
    salary_expectation: str = Field(description="A range with a one-line justification")
    notice_period: str
    work_authorisation: str
    anything_else: str = Field(description="Optional field — 40 words or a graceful skip")


SYSTEM = """You are drafting answers to standard ATS screening questions for one \
candidate and one job.

Rules:
* Use only facts from the supplied profile and resume. No invented detail.
* Answer the question that was asked, then stop. These fields are skim-read.
* No "I am passionate about", no restating the job description back at them.
* salary_expectation: give a range consistent with the stated constraints and add
  one short line of justification. Never say "negotiable" alone — it wastes the
  anchor.
* Be concrete about the notice period and work authorisation. Vagueness here gets
  applications filtered."""


def _safe(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def draft_answers(
    llm: LLM,
    graph: SkillGraph,
    resume: TailoredResume,
    job_title: str,
    company: str,
    jd: str,
    constraints: str,
) -> ScreeningAnswers:
    user = (
        f"=== JOB ===\n{job_title} at {company}\n\n{jd[:3000]}\n\n"
        f"=== CANDIDATE ===\n{resume.headline}\n{resume.summary}\n\n"
        + "\n".join(f"- {b.text}" for b in resume.bullets)
        + f"\n\nSkills: {', '.join(resume.skills_line)}\n\n"
        f"=== HARD CONSTRAINTS (use these verbatim where relevant) ===\n{constraints}"
    )
    return llm.json_call(
        agent="submission",
        tier="cheap",
        system=SYSTEM,
        user=user,
        schema=ScreeningAnswers,
        max_tokens=2000,
    )


def build_packet(
    conn: sqlite3.Connection,
    external_id: str,
    company: str,
    job_title: str,
    job_url: str,
    resume_md: str,
    outreach: OutreachPack | None,
    answers: ScreeningAnswers | None,
    referral_notes: list[str] | None = None,
) -> str:
    """Write the packet to data/out/applications/<company>-<role>/ and return the path."""
    folder = OUT_DIR / "applications" / f"{_safe(company)}-{_safe(job_title)}"
    folder.mkdir(parents=True, exist_ok=True)

    (folder / "resume.md").write_text(resume_md)

    if outreach:
        from .outreach import to_markdown as outreach_md

        (folder / "outreach.md").write_text(outreach_md(outreach, company))

    if answers:
        (folder / "answers.md").write_text(
            "\n".join(
                [
                    f"# Screening answers — {company}",
                    "\n## Why this company\n",
                    answers.why_this_company,
                    "\n## Why this role\n",
                    answers.why_this_role,
                    "\n## Most relevant project\n",
                    answers.biggest_relevant_project,
                    "\n## Salary expectation\n",
                    answers.salary_expectation,
                    "\n## Notice period\n",
                    answers.notice_period,
                    "\n## Work authorisation\n",
                    answers.work_authorisation,
                    "\n## Anything else\n",
                    answers.anything_else,
                ]
            )
        )

    checklist = [
        f"# Submission checklist — {job_title} @ {company}",
        f"\n**Apply at:** {job_url}",
        f"\n**Pipeline id:** `{external_id}`\n",
        "## Before you submit",
        "- [ ] Read `resume.md`. Does every bullet sound like something you could defend in an interview?",
        "- [ ] Check the fabrication warnings from `codeskate tailor` were resolved",
        "- [ ] Convert `resume.md` to PDF with your own template",
        "- [ ] Paste the cover letter from `outreach.md` (only if the form asks for one)",
        "- [ ] Copy screening answers from `answers.md`, adjusting any the form words differently",
    ]
    if referral_notes:
        checklist += [
            "\n## Referral first — do this BEFORE submitting",
            "A referred application is read; a cold one is filtered. Wait 2 days for a reply.",
            *(f"- [ ] {n}" for n in referral_notes),
        ]
    else:
        checklist += [
            "\n## Referral",
            "- [ ] No contacts found for this company. Run `codeskate refer "
            f"{external_id}` after adding people to `config/network.yaml`",
        ]
    checklist += [
        "\n## After you submit",
        f"- [ ] `codeskate stage {external_id} applied`",
        "- [ ] Send the recruiter DM from `outreach.md` if you can find the recruiter",
        "- [ ] The follow-up is due in 6 days — `codeskate next` will remind you",
    ]

    (folder / "checklist.md").write_text("\n".join(checklist))
    return str(folder)
