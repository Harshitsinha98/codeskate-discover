"""Agent 10 — Referral.

Referrals move interview rates more than anything else this system does, so this
agent exists early. What it does *not* do is scrape LinkedIn: that violates their
terms, gets accounts banned, and is not a foundation you can build a company on.

Instead it works from `config/network.yaml`, a file you maintain. Matching is a
free local lookup; the LLM is only used to draft the actual ask.

That trade is deliberate. A smaller, accurate network you curated beats a large
scraped graph you cannot legally use.
"""

from __future__ import annotations

from typing import Any

import yaml

from ..llm import LLM
from ..models import ReferralRequest, SkillGraph
from ..settings import CONFIG_DIR

SYSTEM = """You draft referral requests that are easy to say yes to.

The person you are writing to is doing the candidate a favour and has 30 seconds.

Rules:
* Under 120 words.
* Remind them of the specific shared context in one clause — do not over-explain
  a relationship they remember.
* State the role and company plainly, with the link implied.
* Give them ONE proof point they can repeat to their recruiter. This is the whole
  point: you are writing the sentence they will forward.
* Make the ask precise and small. "Would you be open to submitting me through
  your referral portal?" beats "any help would be great".
* Explicitly remove pressure — one line, at the end, no grovelling.
* For a lukewarm or cold contact, lead with the shared context and be more
  tentative in the ask. For a warm contact, be direct.
* Use only facts from the supplied profile."""


def load_network() -> list[dict[str, Any]]:
    path = CONFIG_DIR / "network.yaml"
    if not path.exists():
        raise SystemExit(
            f"missing {path}\nAdd your contacts there — this agent never scrapes LinkedIn."
        )
    data = yaml.safe_load(path.read_text()) or {}
    return list(data.get("contacts") or [])


def find_contacts(company: str, network: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """Direct contacts at the company, then people who can introduce you there."""
    network = network if network is not None else load_network()
    target = company.strip().lower()

    direct, indirect = [], []
    for c in network:
        if str(c.get("company", "")).strip().lower() == target:
            direct.append({**c, "_route": "direct"})
            continue
        intro_at = [str(x).strip().lower() for x in (c.get("can_intro_at") or [])]
        if target in intro_at:
            indirect.append({**c, "_route": "intro"})

    strength_rank = {"warm": 0, "lukewarm": 1, "cold": 2}
    key = lambda c: strength_rank.get(str(c.get("strength", "cold")).lower(), 3)  # noqa: E731
    return sorted(direct, key=key) + sorted(indirect, key=key)


def run(
    llm: LLM,
    graph: SkillGraph,
    contact: dict[str, Any],
    job_title: str,
    company: str,
    job_url: str,
    top_proof: str,
) -> ReferralRequest:
    route = (
        f"They work at {company} directly."
        if contact.get("_route") == "direct"
        else f"They do not work at {company}, but said they can introduce you there."
    )
    user = (
        f"=== CONTACT ===\n"
        f"Name: {contact.get('name')}\n"
        f"Their role: {contact.get('role', 'unknown')}\n"
        f"Their company: {contact.get('company', 'unknown')}\n"
        f"Relationship: {contact.get('relationship', 'unspecified')}\n"
        f"How you know them: {contact.get('how_you_know', 'unspecified')}\n"
        f"Closeness: {contact.get('strength', 'cold')}\n"
        f"Route: {route}\n\n"
        f"=== THE ROLE ===\n{job_title} at {company}\n{job_url}\n\n"
        f"=== CANDIDATE ===\n"
        f"{graph.candidate_name or 'the candidate'}, {graph.seniority or ''}, "
        f"{graph.total_years_experience:g} years\n"
        f"Strongest proof point for this role: {top_proof}"
    )
    return llm.json_call(
        agent="referral",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=ReferralRequest,
        max_tokens=1200,
    )
