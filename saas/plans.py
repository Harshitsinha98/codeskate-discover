"""Plans and quotas — the cost control that replaced bring-your-own-key.

Why BYOK was wrong: asking a job seeker to create an OpenAI account, add a card
and generate an API key is a wall most people will not climb, and if they are
paying the model provider directly there is very little left to charge them for.
The two models contradict each other. Subscription won.

What that changes: model usage now bills the platform, so a bug or an abusive
account spends real money. Quotas are therefore not a nicety — they are the thing
standing between a runaway loop and a bill. Enforcement happens before every
agent call, and a global daily ceiling sits behind the per-user ones as a circuit
breaker for the case where many accounts misbehave at once.

Quotas are counted in **agent runs**, not dollars. One run is one LLM call. The
user sees "18 of 25 job scores used", because what a call costs is the operator's
problem, not something a job seeker should have to reason about.

Sizing: a run averages roughly $0.004 across the cheap and smart tiers, so the Pro
ceiling of 800 runs caps a single account near $3.20/month against a ₹999 price.
Real usage lands far lower — most runs are scoring, at about $0.0008 each.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    key: str
    name: str
    price_inr: int          # per month, 0 for free
    monthly_runs: int       # LLM-backed agent runs
    max_scores_per_batch: int
    features: tuple[str, ...]
    blocked_agents: frozenset[str] = frozenset()


FREE = Plan(
    key="free",
    name="Free",
    price_inr=0,
    # Enough to build a profile, run gap analysis and score a first batch — the
    # point at which someone can judge whether the matching is any good. Anyone
    # who cannot answer "would I apply to these?" will never convert.
    monthly_runs=40,
    max_scores_per_batch=25,
    features=(
        "Profile built from your resume, with evidence checks",
        "Live openings from India offices of top employers",
        "Match score and a written reason for up to 25 jobs at a time",
        "Honest readiness check on any role you name",
        "40 credits a month",
    ),
    # The expensive, high-value agents are what Pro is for.
    blocked_agents=frozenset({"tailor", "outreach", "prep", "comp", "intel", "contact"}),
)

PRO = Plan(
    key="pro",
    name="Pro",
    price_inr=999,
    monthly_runs=800,
    max_scores_per_batch=200,
    features=(
        "Everything in Free",
        "A resume rewritten for each job — checked so it never claims anything you did not do",
        "Cover letter, recruiter message and hiring-manager email, ready to send",
        "The likely hiring manager to find, with a search link and the note to send",
        "Interview prep: the questions you will get and where you will get caught out",
        "Company briefing before every call",
        "Salary band and a recommended number to ask for",
        "Score up to 200 jobs at a time",
        "800 credits a month",
    ),
)

PLANS: dict[str, Plan] = {FREE.key: FREE, PRO.key: PRO}


def plan_for(key: str | None) -> Plan:
    return PLANS.get((key or "free").lower(), FREE)


def global_daily_run_cap() -> int:
    """Circuit breaker across all accounts.

    Per-user quotas stop one account running away. This stops a bad deploy, a
    retry storm, or a hundred accounts misbehaving at once from producing a bill
    nobody sees until the statement arrives.
    """
    return int(os.getenv("GLOBAL_DAILY_RUN_CAP", "5000"))


BLURBS = {
    "free": "Enough to see your real matches and decide whether this is any good.",
    "pro": "For when you are actually applying — everything unlocked, every week.",
}


def catalogue() -> list[dict]:
    """Plan data for the pricing page and the in-app upgrade card.

    `monthly_runs` is surfaced as **credits** in every user-facing string. "Agent
    run" is an implementation detail: it tells a job seeker nothing, and the first
    round of feedback on this product was that it read like a developer's notes
    rather than a product.
    """
    return [
        {
            "key": p.key,
            "name": p.name,
            "price_inr": p.price_inr,
            "credits": p.monthly_runs,
            "monthly_runs": p.monthly_runs,  # kept for older clients
            "blurb": BLURBS[p.key],
            "features": list(p.features),
        }
        for p in (FREE, PRO)
    ]
