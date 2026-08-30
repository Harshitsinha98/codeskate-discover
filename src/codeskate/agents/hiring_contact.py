"""Agent — Find the hiring manager.

The most valuable thing this whole system can do for a career changer is get their
application in front of a human instead of an applicant-tracking filter. Referrals
and direct-to-hiring-manager notes move interview rates more than anything else.

What this agent will NOT do, on purpose:

  * Scrape LinkedIn. It breaks their terms, gets accounts banned, and is not a
    foundation you can build a company on.
  * Guess an email address. `firstname@company.com` is wrong ~40% of the time,
    and sending "I know you're hiring for X" to the wrong person damages the
    candidate's reputation, not ours.

So it does the 90% that is legal and reliable: it reads the job description,
works out *which role* owns this hire (the manager the position reports into),
builds a LinkedIn people-search URL scoped to that title and company, orders the
ways to reach that person warmest-first, and writes the short connection note.
The candidate does the last 10% — clicking the link, finding the actual human,
verifying it — which is exactly the part that cannot be automated without
crossing the line above.

The URL is a plain search link, not scraped data: constructing a query string is
no different from typing it into the search box yourself.
"""

from __future__ import annotations

from urllib.parse import quote_plus

from ..llm import LLM
from ..models import HiringContactPlan

SYSTEM = """You identify who owns a specific hire and how a candidate should reach them.

You are given ONE job posting. Work out the reporting line, then produce targets.

Rules:
* Name TITLES, never invent a person's name. You do not know who holds the role.
* The primary target is the HIRING MANAGER — the person this role reports to. For
  an IC support/SRE/engineering role that is usually a team lead or an engineering
  manager for that function; infer the function from the JD, not a generic guess.
* Add a skip-level (the manager's manager) only when the JD implies a small team
  or a senior IC hire where the director is plausibly involved.
* Add the recruiter as a target only if the JD names a talent/recruiting contact
  or clearly routes through one.
* seniority must be exactly one of: hiring_manager, skip_level, recruiter, peer.

routes: order them warmest-first. A mutual connection beats the company's public
team page, which beats a cold LinkedIn message. Be concrete about each.

connection_note: under 300 characters, for a LinkedIn connection request.
* One proof point from the candidate, tied to what THIS team does.
* One small, precise ask ("open to a quick note about the X role?").
* No "I am passionate about". No pressure. Peer tone.
* Use only facts from the candidate profile supplied.

confidence: high if the JD clearly states the team/function, low if you are
inferring the reporting line from a vague posting."""


def _linkedin_url(company: str, titles: list[str]) -> str:
    """A LinkedIn people-search URL scoped to the company and the target titles.

    Built here rather than by the model so it is always well-formed and never a
    hallucinated link. LinkedIn's people search takes a free-text `keywords`
    parameter; company plus the most likely title is the query a person would type
    themselves.
    """
    primary = titles[0] if titles else "engineering manager"
    keywords = f"{company} {primary}"
    return f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(keywords)}"


def run(
    llm: LLM,
    candidate_name: str,
    seniority: str,
    years: float,
    top_proof: str,
    job_title: str,
    company: str,
    jd: str,
) -> HiringContactPlan:
    user = (
        f"=== JOB ===\n{job_title} at {company}\n\n{jd[:3500]}\n\n"
        f"=== CANDIDATE ===\n"
        f"{candidate_name or 'the candidate'}, {seniority or 'unspecified'}, "
        f"{years:g} years\n"
        f"Strongest proof point for this role: {top_proof}"
    )
    plan = llm.json_call(
        agent="hiring_contact",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=HiringContactPlan,
        max_tokens=1500,
    )

    # Overwrite whatever the model produced for the URL with a URL we constructed,
    # so it is guaranteed valid and scoped to the titles the model actually chose.
    titles = [t.likely_title for t in plan.targets] or [job_title]
    plan.linkedin_search_url = _linkedin_url(company, titles)
    return plan
