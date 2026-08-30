"""Agent 9 — Cover Letter & Outreach.

Produces the four things you actually need per application: a cover letter, a
short recruiter message, a hiring-manager email, and the follow-up you will send
a week later when nobody replies.

Same factual constraint as the tailoring agent: it writes from the skill graph and
the tailored resume, never beyond them.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import OutreachPack, SkillGraph, TailoredResume

SYSTEM = """You write job-application outreach that busy people actually reply to.

Hard rules:
* Use ONLY facts present in the candidate profile and tailored resume supplied.
  No invented projects, metrics, employers or enthusiasm about things they have
  not done.
* Never write "I am passionate about" or "I was excited to see". Open with
  something specific to this company or role.
* Lead with the single most relevant proof point for THIS job, with its metric.
* No apologising for gaps. No begging. Peer-to-peer tone.

Format constraints:
* cover_letter: 150-200 words, 3 short paragraphs. Specific > enthusiastic.
* recruiter_dm: under 400 characters. Assume they read 200 of these a day.
  One proof point, one clear ask.
* hm_email_subject: 6-9 words, concrete, no clickbait.
* hm_email: under 120 words. A hiring manager reads this on a phone between
  meetings. One proof point tied to a problem their team plausibly has.
* followup_message: under 60 words, sent 5-7 days later. Add one new piece of
  value rather than just 'bumping this'."""


def run(
    llm: LLM,
    graph: SkillGraph,
    resume: TailoredResume,
    job_title: str,
    company: str,
    jd: str,
) -> OutreachPack:
    bullets = "\n".join(f"- {b.text}" for b in resume.bullets)
    user = (
        f"=== JOB ===\n{job_title} at {company}\n\n{jd[:3500]}\n\n"
        f"=== CANDIDATE ===\n"
        f"{graph.candidate_name or 'the candidate'}, "
        f"{graph.seniority or 'unspecified'}, {graph.total_years_experience:g} years\n\n"
        f"=== TAILORED RESUME FOR THIS ROLE ===\n"
        f"{resume.headline}\n{resume.summary}\n\n{bullets}\n\n"
        f"Skills claimed: {', '.join(resume.skills_line)}"
    )
    return llm.json_call(
        agent="outreach",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=OutreachPack,
        max_tokens=3000,
    )


def to_markdown(pack: OutreachPack, company: str) -> str:
    return "\n".join(
        [
            f"# Outreach — {company}",
            "\n## Cover letter\n",
            pack.cover_letter,
            "\n## Recruiter DM",
            f"*({len(pack.recruiter_dm)} chars)*\n",
            pack.recruiter_dm,
            "\n## Hiring manager email",
            f"**Subject:** {pack.hm_email_subject}\n",
            pack.hm_email,
            "\n## Follow-up (send after 5-7 days of silence)\n",
            pack.followup_message,
        ]
    )
