"""Agent 14 — Negotiation.

The highest-leverage twenty minutes of the whole job hunt. A 10% improvement here
outweighs every efficiency gain the other seventeen agents produce, which is why
the Career Manager escalates an offer above everything else.

Gives you a word-for-word counter script, because that is the part people
actually fail at — not knowing the theory, but freezing on the call.
"""

from __future__ import annotations

from ..llm import LLM
from ..models import CompEstimate, NegotiationPlan, SkillGraph

SYSTEM = """You are advising a candidate on a live job offer.

Rules:
1. `assessment`: say plainly whether the offer is strong, fair or weak against the
   band supplied. If the band is low-confidence, factor that in rather than
   pretending to precision you don't have.
2. `counter_script`: word-for-word what to say or write. Short, warm, unapologetic,
   with a specific number. No hedging, no "I was hoping maybe". This gets used
   verbatim under pressure, so it must sound like a person.
3. `leverage`: only real leverage. Competing offers, a scarce skill they need, a
   scoped start date. Do not manufacture leverage — bluffing about an offer that
   doesn't exist can lose the role outright, and you must not suggest it.
4. `concessions`: what to trade if they hold firm on base — signing bonus, start
   date, review timing, title, remote days, equity refresh.
5. `walk_away`: the honest floor, given their stated constraints.
6. `risks`: name the ways this negotiation could go wrong, including the risk of
   pushing a small company past its actual budget.
7. Never advise lying. Not about current salary, not about competing offers."""


def run(
    llm: LLM,
    graph: SkillGraph,
    company: str,
    job_title: str,
    offer_details: str,
    comp: CompEstimate | None,
    constraints: str = "",
) -> NegotiationPlan:
    band = (
        f"MARKET BAND (confidence: {comp.confidence}): "
        f"base {comp.base_low:g}-{comp.base_high:g} {comp.unit}, "
        f"total {comp.total_low:g}-{comp.total_high:g} {comp.unit}. "
        f"Suggested ask: {comp.recommended_ask:g}. Basis: {comp.basis}"
        if comp
        else "No market band available — say so in your assessment and be more "
        "cautious about naming a hard number."
    )
    user = (
        f"OFFER FROM: {company} — {job_title}\n\n"
        f"=== THE OFFER AS STATED ===\n{offer_details}\n\n"
        f"=== {band}\n\n"
        f"=== CANDIDATE ===\n{graph.candidate_name or 'candidate'}, "
        f"{graph.seniority or 'unknown'}, {graph.total_years_experience:g} years\n"
        + (f"\nCONSTRAINTS:\n{constraints}" if constraints else "")
    )
    return llm.json_call(
        agent="negotiation",
        tier="smart",
        system=SYSTEM,
        user=user,
        schema=NegotiationPlan,
        max_tokens=3000,
    )


def to_markdown(plan: NegotiationPlan, company: str) -> str:
    def block(title: str, items: list[str]) -> list[str]:
        return [f"\n## {title}", *(f"- {i}" for i in items)] if items else []

    return "\n".join(
        [
            f"# Negotiation plan — {company}",
            "\n## Assessment",
            plan.assessment,
            f"\n## Target\n{plan.target}",
            *block("Your leverage", plan.leverage),
            "\n## Counter script — use this close to verbatim\n",
            "> " + plan.counter_script.replace("\n", "\n> "),
            *block("If they hold firm, trade for", plan.concessions),
            f"\n## Walk-away point\n{plan.walk_away}",
            *block("Risks", plan.risks),
        ]
    )
