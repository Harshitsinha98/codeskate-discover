"""Agent 7 — Compensation.

Estimates the band for a role so you don't anchor yourself low.

Order of preference, which matters more than the model quality here:
  1. A range stated in the job description — real data, extracted with regex.
  2. An LLM estimate, clearly labelled low-confidence.

Do not mistake (2) for market data. It is a prior, useful for avoiding an
obviously bad anchor, not a number to quote as fact. A real product would join
this against an actual compensation dataset; that is a data-licensing problem,
not a prompting problem.
"""

from __future__ import annotations

import re

from ..llm import LLM
from ..models import CompEstimate

SYSTEM = """You estimate a realistic compensation band for one role, for one candidate.

Rules:
1. If the job description states a range, that range is the truth. Anchor to it
   and set confidence to "high".
2. Otherwise estimate from role, seniority, location and company type. Set
   confidence to "low" unless the role is extremely standardised.
3. `basis` must honestly describe what the estimate rests on, including its
   weaknesses. If you are extrapolating, say that plainly.
4. `recommended_ask` should sit in the upper part of the band but stay defensible
   — high enough to leave negotiating room, not high enough to get screened out.
5. Use LPA for India, and annual local currency elsewhere.
6. Do not invent precision. Round to sensible increments."""

# Matches "₹18-25 LPA", "18 - 25 LPA", "$120,000 - $160,000", "INR 2,000,000"
_PATTERNS = [
    r"(?:₹|INR|Rs\.?)\s*(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(?:₹|INR|Rs\.?)?\s*(\d+(?:\.\d+)?)\s*(?:LPA|lakh|lac)",
    r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:LPA|lakhs?\s*per\s*annum)",
    r"\$\s*(\d[\d,]{4,})\s*(?:-|–|to)\s*\$?\s*(\d[\d,]{4,})",
]


def extract_stated_comp(jd: str) -> tuple[float, float, str] | None:
    """Pull a stated range out of the posting. Free and exact where it works."""
    for pattern in _PATTERNS:
        m = re.search(pattern, jd, re.IGNORECASE)
        if not m:
            continue
        low = float(m.group(1).replace(",", ""))
        high = float(m.group(2).replace(",", ""))
        if low <= 0 or high < low:
            continue
        unit = "USD" if "$" in m.group(0) else "LPA"
        return low, high, unit
    return None


def run(
    llm: LLM,
    job_title: str,
    company: str,
    location: str | None,
    jd: str,
    years_experience: float,
    seniority: str | None,
    constraints: str = "",
) -> CompEstimate:
    stated = extract_stated_comp(jd)
    stated_line = (
        f"THE POSTING STATES A RANGE: {stated[0]}-{stated[1]} {stated[2]}. "
        "Anchor to this and set confidence high.\n\n"
        if stated
        else "The posting does not state a range.\n\n"
    )
    user = (
        f"{stated_line}"
        f"ROLE: {job_title}\nCOMPANY: {company}\nLOCATION: {location or 'not stated'}\n"
        f"CANDIDATE: {years_experience:g} years, seniority {seniority or 'unknown'}\n"
        + (f"CANDIDATE CONSTRAINTS:\n{constraints}\n" if constraints else "")
        + f"\n=== JOB DESCRIPTION ===\n{jd[:3500]}"
    )
    return llm.json_call(
        agent="compensation",
        tier="cheap",
        system=SYSTEM,
        user=user,
        schema=CompEstimate,
        max_tokens=1200,
    )
