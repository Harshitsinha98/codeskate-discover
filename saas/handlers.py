"""Job handlers — one unit of work each.

Every handler calls the same agent functions the CLI does. Nothing about the
agents changed for the hosted version; only where their state lives and how the
work is sliced.

Planners run inline at enqueue time and must stay free and fast: reading config,
loading a profile, applying the rule-based prefilter. No LLM call belongs in a
planner, because the user is waiting on that HTTP request.
"""

from __future__ import annotations

from typing import Any

import yaml

from codeskate.agents import (
    company_intel,
    compensation,
    discovery,
    fit_scoring,
    gap_analysis,
    interview_prep,
    outreach,
    pipeline,
    resume_tailoring,
    skill_graph,
)
from codeskate.models import CompanyIntel, OutreachPack, SkillGraph, TailoredResume
from codeskate.settings import CONFIG_DIR

from . import queue, store
from .runtime import user_llm


def _graph(user_id: int) -> SkillGraph:
    raw = store.load_profile(user_id)
    if not raw:
        raise RuntimeError("Build your skill graph first")
    return SkillGraph.model_validate(raw)


def _targets(user_id: int) -> dict:
    """User's own targets, falling back to the shipped defaults."""
    saved = store.load_targets(user_id)
    if saved:
        return saved
    return yaml.safe_load((CONFIG_DIR / "targets.yaml").read_text()) or {}


def _posting_or_fail(external_id: str) -> dict:
    posting = store.posting(external_id)
    if not posting:
        raise RuntimeError("That job posting is no longer available")
    return posting


# --------------------------------------------------------------------------- #
# Agents 1 + 2 — profile
# --------------------------------------------------------------------------- #


def profile_handler(user_id: int, payload: dict, index: int) -> dict:
    bundle = store.document_bundle(user_id)
    if not bundle.strip():
        raise RuntimeError("Upload a resume and brag document first")

    llm = user_llm(user_id)
    graph = skill_graph.build(llm, bundle)
    store.save_profile(user_id, graph.model_dump(mode="json"))
    proven = len(graph.claimable_skills())
    return {
        "log": [
            f"read {len(bundle):,} characters of documents",
            f"extracted {len(graph.skills)} skills, {proven} evidence-backed",
            f"{len(graph.achievements)} achievements recorded",
        ],
        "result": {"skills": len(graph.skills), "proven": proven,
                   "achievements": len(graph.achievements)},
    }


# --------------------------------------------------------------------------- #
# Agent 3 — gaps
# --------------------------------------------------------------------------- #


def gaps_handler(user_id: int, payload: dict, index: int) -> dict:
    graph = _graph(user_id)
    role = payload["role"]
    llm = user_llm(user_id)
    report = gap_analysis.run(llm, graph, role, fit_scoring.constraints_text(_targets(user_id)))
    data = report.model_dump(mode="json")
    store.save_gap_report(user_id, role, data)
    blocking = sum(1 for g in report.gaps if g.severity == "blocking")
    return {
        "log": [f"readiness {report.readiness_pct}%", f"{blocking} blocking gap(s)"],
        "result": data,
    }


# --------------------------------------------------------------------------- #
# Agent 4 — discovery, one board per unit
# --------------------------------------------------------------------------- #


def discover_planner(user_id: int, payload: dict) -> dict:
    """Flatten the configured boards into one unit each.

    Chunking here is what makes discovery survive a function timeout: fetching
    every board took 2-3 minutes as a single operation, while one board is a few
    seconds. Workday boards are slower because each posting needs a second request
    for its description, so they are worth isolating.
    """
    # The user's own board list if they have configured one, otherwise the shipped
    # default. Postings themselves stay shared across all users.
    own = store.load_boards(user_id)
    config = own or discovery.load_company_config()
    units = [
        {"ats": ats, "entry": entry}
        for ats, entries in config.items()
        for entry in entries
    ]
    source = "your company list" if own else "the default company list"
    return {
        "payload": {"units": units},
        "log": [f"planned {len(units)} board(s) from {source} — free, no LLM calls"],
    }


def discover_handler(user_id: int, payload: dict, index: int) -> dict:
    unit = payload["units"][index]
    ats, entry = unit["ats"], unit["entry"]
    label = entry["tenant"] if isinstance(entry, dict) else entry

    postings, errors = discovery.fetch_all({ats: [entry]})
    postings = discovery.dedupe(postings)
    new = store.upsert_postings(postings)

    line = f"{ats}/{label}: {len(postings)} postings, {new} new"
    if errors:
        line = f"{ats}/{label}: unreachable ({errors[0].split(': ')[-1]})"
    return {"log": [line], "result": {"total_postings": store.postings_count()}}


# --------------------------------------------------------------------------- #
# Agent 5 — scoring, one posting per unit
# --------------------------------------------------------------------------- #


def score_planner(user_id: int, payload: dict) -> dict:
    """Apply the free prefilter now, so units are only the postings worth paying for."""
    if not store.load_profile(user_id):
        raise RuntimeError("Build your skill graph first")

    targets = _targets(user_id)
    candidates = store.unscored_postings(user_id, 5000)
    kept = fit_scoring.prefilter(candidates, targets)
    limit = int(payload.get("limit", 25))
    selected = [j["external_id"] for j in kept[:limit]]

    return {
        "payload": {"units": selected},
        "log": [
            f"prefilter (free): {len(candidates)} unscored -> {len(kept)} plausible",
            f"queued {len(selected)} for scoring",
        ],
    }


def score_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["units"][index]
    posting = store.posting(external_id)
    if not posting:
        return {"log": [f"{external_id}: posting vanished, skipped"]}

    graph = _graph(user_id)
    targets = _targets(user_id)
    llm = user_llm(user_id)

    result = fit_scoring.score_job(
        llm, skill_graph.profile_brief(graph), posting, fit_scoring.constraints_text(targets)
    )
    store.save_fit_score(user_id, external_id, result.model_dump(mode="json"))
    return {
        "log": [f"{result.score:>3} [{result.verdict}] {posting['company']} — "
                f"{posting['title'][:44]}"],
        "result": {"scored": index + 1},
    }


# --------------------------------------------------------------------------- #
# Agent 8 — tailoring
# --------------------------------------------------------------------------- #


def tailor_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["external_id"]
    posting = _posting_or_fail(external_id)
    graph = _graph(user_id)

    if not graph.achievements:
        raise RuntimeError(
            "Your profile has no achievements. This agent only rearranges facts you "
            "supplied — add quantified accomplishments to your brag document and "
            "rebuild your skill graph."
        )

    store.add_application(user_id, external_id)
    llm = user_llm(user_id)
    resume = resume_tailoring.run(
        llm, graph, posting["title"], posting["company"], posting["description"] or ""
    )
    violations = resume_tailoring.verify(resume, graph)
    store.save_artifact(user_id, "resume", resume.model_dump(mode="json"), external_id)

    app = store.get_application(user_id, external_id)
    if app and pipeline.can_move(app["stage"], "tailored"):
        store.set_stage(user_id, external_id, "tailored", "resume generated")
        store.record_outcome(user_id, "tailored", external_id=external_id)

    return {
        "log": [f"{len(resume.bullets)} bullets written",
                f"fabrication check: {len(violations)} violation(s)"],
        "result": {"resume": resume.model_dump(mode="json"), "violations": violations},
    }


# --------------------------------------------------------------------------- #
# Agent 9 — outreach
# --------------------------------------------------------------------------- #


def outreach_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["external_id"]
    posting = _posting_or_fail(external_id)
    stored = store.latest_artifact(user_id, "resume", external_id)
    if not stored:
        raise RuntimeError("Tailor the resume for this job first")

    llm = user_llm(user_id)
    pack = outreach.run(
        llm, _graph(user_id), TailoredResume.model_validate(stored),
        posting["title"], posting["company"], posting["description"] or "",
    )
    data = pack.model_dump(mode="json")
    store.save_artifact(user_id, "outreach", data, external_id)
    return {"log": [f"recruiter DM is {len(pack.recruiter_dm)} characters"], "result": data}


# --------------------------------------------------------------------------- #
# Agent 12 — interview prep
# --------------------------------------------------------------------------- #


def prep_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["external_id"]
    posting = _posting_or_fail(external_id)

    cached = store.load_company_intel(posting["company"])
    intel = CompanyIntel.model_validate(cached) if cached else None

    llm = user_llm(user_id)
    brief = interview_prep.run(
        llm, _graph(user_id), posting["title"], posting["company"],
        posting["description"] or "", intel, payload.get("stage", "screen"),
    )
    data = brief.model_dump(mode="json")
    store.save_artifact(user_id, "prep_brief", data, external_id)
    return {
        "log": [f"{len(brief.likely_questions)} questions, "
                f"{len(brief.star_stories)} STAR stories, "
                f"{len(brief.weak_spots)} weak spot(s)"],
        "result": data,
    }


# --------------------------------------------------------------------------- #
# Agent 6 — company intel (shared cache across all users)
# --------------------------------------------------------------------------- #


def intel_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["external_id"]
    posting = _posting_or_fail(external_id)

    cached = store.load_company_intel(posting["company"])
    if cached:
        return {"log": [f"served from shared cache for {posting['company']} — no cost"],
                "result": cached}

    llm = user_llm(user_id)
    intel = company_intel.run(
        llm, posting["company"], posting["title"], posting["description"] or ""
    )
    data = intel.model_dump(mode="json")
    store.save_company_intel(posting["company"], data)
    return {"log": [f"briefing built, confidence {intel.confidence}"], "result": data}


# --------------------------------------------------------------------------- #
# Agent 7 — compensation
# --------------------------------------------------------------------------- #


def comp_handler(user_id: int, payload: dict, index: int) -> dict:
    external_id = payload["external_id"]
    posting = _posting_or_fail(external_id)
    graph = _graph(user_id)

    stated = compensation.extract_stated_comp(posting["description"] or "")
    log = [f"posting states {stated[0]:g}-{stated[1]:g} {stated[2]} (extracted free)"] if stated else []

    llm = user_llm(user_id)
    estimate = compensation.run(
        llm, posting["title"], posting["company"], posting["location"],
        posting["description"] or "", graph.total_years_experience, graph.seniority,
        fit_scoring.constraints_text(_targets(user_id)),
    )
    data = estimate.model_dump(mode="json")
    store.save_comp_estimate(user_id, external_id, data)
    log.append(f"band {estimate.base_low:g}-{estimate.base_high:g} {estimate.unit}, "
               f"confidence {estimate.confidence}")
    return {"log": log, "result": data}


# --------------------------------------------------------------------------- #
# registration
# --------------------------------------------------------------------------- #

queue.register("profile", profile_handler)
queue.register("gaps", gaps_handler)
queue.register("discover", discover_handler, discover_planner)
queue.register("score", score_handler, score_planner)
queue.register("tailor", tailor_handler)
queue.register("outreach", outreach_handler)
queue.register("prep", prep_handler)
queue.register("intel", intel_handler)
queue.register("comp", comp_handler)

SINGLE_JOB_KINDS = {"tailor", "outreach", "prep", "intel", "comp"}
