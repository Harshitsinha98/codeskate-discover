"""Agent 5 — Fit Scoring.

Two stages, on purpose:

  Stage 1 (free)  rule-based prefilter on title, location and seniority.
                  Throws away the obvious 80-90% at zero cost.
  Stage 2 (paid)  LLM scores only the survivors, and must justify the score.

Sending 500 raw JDs to an LLM works but costs ~10x more for worse results.
The prefilter is the whole reason this stays cheap.
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

import yaml

from ..llm import LLM
from ..models import FitScore
from ..settings import CONFIG_DIR

SYSTEM = """You are a hiring-side evaluator scoring how well ONE candidate fits ONE job.

Score 0-100 on realistic callback probability, not aspiration:
  80-100 strong  - candidate clears the bar; recruiter would advance them
  55-79   stretch - plausible with a strong referral or tailored application
  0-54    weak    - would be filtered out; do not waste an application

Weigh these, in order:
1. Hard requirements: years of experience, must-have tech, location/work authorisation.
   A hard miss caps the score at 54 no matter how good the rest looks.
2. Core stack overlap with the candidate's level-3+ skills.
3. Seniority alignment. Both over- and under-qualified are bad fits.
4. Transferability of adjacent skills.

matched_skills / missing_skills must name skills that literally appear in the job
description. Keep reasoning to 2-3 concrete sentences. No encouragement, no filler
— this feeds a decision about where to spend limited application effort."""


def load_targets() -> dict[str, Any]:
    path = CONFIG_DIR / "targets.yaml"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    return yaml.safe_load(path.read_text()) or {}


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())


def prefilter(jobs: list[sqlite3.Row], targets: dict[str, Any]) -> list[sqlite3.Row]:
    """Free, deterministic filter. Cheap to run, cheap to tune."""
    include = [t.lower() for t in targets.get("title_include", [])]
    exclude = [t.lower() for t in targets.get("title_exclude", [])]
    locations = [l.lower() for l in targets.get("locations", [])]
    remote_ok = bool(targets.get("remote_ok", True))
    remote_exclude = [r.lower() for r in targets.get("remote_exclude_regions", [])]
    min_desc = int(targets.get("min_description_chars", 200))

    kept = []
    for job in jobs:
        title = _norm(job["title"])
        if include and not any(k in title for k in include):
            continue
        if any(k in title for k in exclude):
            continue
        if len(job["description"] or "") < min_desc:
            continue

        if locations:
            loc = _norm(job["location"])
            in_target = any(l in loc for l in locations)
            # "Remote" is region-scoped in practice: "Remote, USA" is useless to a
            # candidate who cannot work in the USA. Only treat remote as a match
            # when it isn't pinned to a region we're excluded from.
            is_remote = "remote" in loc or "remote" in title
            wrong_region = any(r in loc for r in remote_exclude)
            if not (in_target or (remote_ok and is_remote and not wrong_region)):
                continue

        kept.append(job)
    return kept


def score_job(llm: LLM, profile_brief: str, job: sqlite3.Row, constraints: str) -> FitScore:
    """Profile + constraints go in the cached system prefix; only the JD varies."""
    system = f"{SYSTEM}\n\n=== CANDIDATE ===\n{profile_brief}\n\n=== CONSTRAINTS ===\n{constraints}"
    user = (
        f"=== JOB ===\n"
        f"Company: {job['company']}\n"
        f"Title: {job['title']}\n"
        f"Location: {job['location'] or 'not stated'}\n\n"
        f"Description:\n{(job['description'] or '')[:5000]}"
    )
    return llm.json_call(
        agent="fit_scoring",
        tier="cheap",  # high volume: the cheap tier is plenty for this
        system=system,
        user=user,
        schema=FitScore,
        max_tokens=800,
        cache_system=True,
    )


def constraints_text(targets: dict[str, Any]) -> str:
    c = targets.get("constraints", {}) or {}
    return "\n".join(f"- {k.replace('_', ' ')}: {v}" for k, v in c.items()) or "- none stated"
