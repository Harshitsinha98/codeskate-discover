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


def _desc_len(job: Any) -> int:
    """Description length, whether the row carries the body or just its length.

    Callers pass lightweight rows (external_id, title, location, desc_len) when
    scanning every unscored posting, and full rows when scoring. Both work.
    """
    try:
        value = job["desc_len"]
    except (KeyError, IndexError, TypeError):
        value = None
    if value is not None:
        return int(value)
    return len(job["description"] or "")


def _norm(s: str | None) -> str:
    """Lowercase, strip punctuation, and pad with spaces.

    The padding lets a config pattern use explicit word boundaries — " sr " will
    match "Sr. Software Engineer" without also matching "usr" or "disaster".
    """
    cleaned = re.sub(r"[^a-z0-9 ]", " ", (s or "").lower())
    return f" {re.sub(r'  +', ' ', cleaned).strip()} "


class _Rules:
    """The prefilter's configuration, resolved once instead of per posting."""

    __slots__ = ("include", "exclude", "locations", "remote_ok", "remote_exclude", "min_desc")

    def __init__(self, targets: dict[str, Any]) -> None:
        self.include = [t.lower() for t in targets.get("title_include", [])]
        self.exclude = [t.lower() for t in targets.get("title_exclude", [])]
        self.locations = [l.lower() for l in targets.get("locations", [])]
        self.remote_ok = bool(targets.get("remote_ok", True))
        self.remote_exclude = [r.lower() for r in targets.get("remote_exclude_regions", [])]
        self.min_desc = int(targets.get("min_description_chars", 200))


# Rejection reasons, in the order they are tested. Ordering matters for the
# report: a posting is attributed to the first rule that rejected it, so the
# counts add up to the total instead of double-counting.
TITLE_MISS = "title_not_relevant"
TITLE_EXCLUDED = "title_excluded"
DESC_SHORT = "description_too_short"
LOCATION = "location_outside_target"

REASON_LABELS = {
    TITLE_MISS: "Title is not one of the roles you asked for",
    TITLE_EXCLUDED: "Title matched one of your exclusions",
    DESC_SHORT: "Posting has almost no description to score against",
    LOCATION: "Location is outside the places you chose",
}


def _reject(job: Any, r: _Rules) -> tuple[str, str] | None:
    """Why this posting was dropped, or None if it survived.

    Split out of prefilter so the free filter and the explanation of the free
    filter can never disagree. Returning the offending term as well as the reason
    is what lets the UI say "'senior' removed 240 postings" instead of the useless
    "no matches found".
    """
    title = _norm(job["title"])

    if r.include and not any(k in title for k in r.include):
        return TITLE_MISS, ""
    for k in r.exclude:
        if k in title:
            return TITLE_EXCLUDED, k.strip()
    if _desc_len(job) < r.min_desc:
        return DESC_SHORT, ""

    if r.locations:
        loc = _norm(job["location"])
        in_target = any(l in loc for l in r.locations)
        is_remote = "remote" in loc or "remote" in title
        if not (
            in_target
            or (r.remote_ok and is_remote and _remote_is_open(loc, r.locations, r.remote_exclude))
        ):
            return LOCATION, (job["location"] or "not stated").strip()

    return None


def prefilter(jobs: list[sqlite3.Row], targets: dict[str, Any]) -> list[sqlite3.Row]:
    """Free, deterministic filter. Cheap to run, cheap to tune."""
    rules = _Rules(targets)
    return [job for job in jobs if _reject(job, rules) is None]


def report(jobs: list[sqlite3.Row], targets: dict[str, Any]) -> dict[str, Any]:
    """Run the filter and explain the outcome. Free — no model is involved.

    This exists because of a specific failure: a user saw
    `prefilter: 1368 unscored -> 0 plausible` and concluded the product was
    broken. It was not — the postings were real and the filter was working, the
    titles just did not match. An empty result and a bug are indistinguishable
    unless the system says which rule emptied it, so it now does.
    """
    rules = _Rules(targets)

    kept: list[Any] = []
    counts: dict[str, int] = {}
    terms: dict[str, int] = {}
    places: dict[str, int] = {}
    # Postings that only failed on location are the interesting near miss: the
    # role is right, so widening the city list would surface them immediately.
    right_role_wrong_place: list[dict] = []
    excluded_samples: list[dict] = []

    for job in jobs:
        verdict = _reject(job, rules)
        if verdict is None:
            kept.append(job)
            continue

        reason, detail = verdict
        counts[reason] = counts.get(reason, 0) + 1
        if reason == TITLE_EXCLUDED:
            terms[detail] = terms.get(detail, 0) + 1
            if len(excluded_samples) < 8:
                excluded_samples.append(
                    {"title": job["title"], "company": job["company"], "term": detail}
                )
        elif reason == LOCATION:
            places[detail] = places.get(detail, 0) + 1
            if len(right_role_wrong_place) < 12:
                right_role_wrong_place.append(
                    {"title": job["title"], "company": job["company"],
                     "location": job["location"]}
                )

    def top(d: dict[str, int], n: int = 6) -> list[dict]:
        return [
            {"value": k, "count": v}
            for k, v in sorted(d.items(), key=lambda kv: -kv[1])[:n]
        ]

    return {
        "total": len(jobs),
        "kept": len(kept),
        "rejected": [
            {"reason": reason, "label": REASON_LABELS[reason], "count": counts[reason]}
            for reason in (TITLE_MISS, TITLE_EXCLUDED, DESC_SHORT, LOCATION)
            if counts.get(reason)
        ],
        "top_exclusion_terms": top(terms),
        "top_rejected_locations": top(places),
        "right_role_wrong_place": right_role_wrong_place,
        "excluded_samples": excluded_samples,
        "advice": _advice(len(jobs), len(kept), counts, terms, places),
    }


def _advice(total: int, kept: int, counts: dict[str, int],
            terms: dict[str, int], places: dict[str, int]) -> list[dict]:
    """One or two concrete things to change, ordered by how much they would recover.

    Not generic tips. Each entry names the rule, the cost of that rule in
    postings, and the specific edit — otherwise it is decoration.
    """
    out: list[dict] = []

    if total == 0:
        return [{
            "problem": "There are no job postings loaded yet.",
            "fix": "Run a job search first — it is free and takes about a minute.",
            "action": "discover",
        }]

    if kept >= 15:
        return out

    ranked = sorted(counts.items(), key=lambda kv: -kv[1])
    for reason, count in ranked[:2]:
        share = round(count / total * 100)
        if reason == TITLE_MISS:
            out.append({
                "problem": f"{count:,} postings ({share}%) are for roles you did not "
                           "ask for — usually a sign the chosen role types are too narrow "
                           "for the companies being searched.",
                "fix": "Add another role type, or add more companies to search.",
                "action": "roles",
            })
        elif reason == TITLE_EXCLUDED:
            worst = sorted(terms.items(), key=lambda kv: -kv[1])[:3]
            named = ", ".join(f"'{t}' ({c})" for t, c in worst)
            out.append({
                "problem": f"{count:,} postings ({share}%) were removed by your own "
                           f"exclusions: {named}.",
                "fix": "If any of those are jobs you would actually take, remove that "
                       "word from the exclusion list.",
                "action": "filters",
            })
        elif reason == LOCATION:
            worst = ", ".join(f"{p} ({c})" for p, c in
                              sorted(places.items(), key=lambda kv: -kv[1])[:3])
            out.append({
                "problem": f"{count:,} postings ({share}%) are the right kind of role "
                           f"in a city you did not select: {worst}.",
                "fix": "Add those cities, or select 'Anywhere in India'.",
                "action": "cities",
            })
        elif reason == DESC_SHORT:
            out.append({
                "problem": f"{count:,} postings ({share}%) have almost no description, "
                           "so there is nothing to score against.",
                "fix": "Nothing to do — these are usually placeholder listings and are "
                       "correctly skipped.",
                "action": "",
            })
    return out


# Words that describe the arrangement rather than the place.
_ARRANGEMENT = {
    "remote", "remotely", "friendly", "first", "hybrid", "onsite", "on", "site",
    "office", "flexible", "anywhere", "global", "worldwide", "distributed", "or",
    "and", "based", "in", "eligible", "locations", "location", "multiple",
}


def _remote_is_open(loc: str, locations: list[str], explicit_exclude: list[str]) -> bool:
    """Is this remote role open to our region?

    "Remote" alone is genuinely global. "Remote - Poland" is not, and neither is
    "Boston, MA; Remote-Friendly". Enumerating every excluded country is a losing
    game, so instead: strip the arrangement words and see whether any *place* is
    named. If one is, it has to be a place we target.
    """
    if any(r in loc for r in explicit_exclude):
        return False

    remainder = {w for w in loc.split() if w not in _ARRANGEMENT and not w.isdigit()}
    if not remainder:
        return True  # "Remote" with no region attached — open to anyone

    targets = {w for loc_name in locations for w in loc_name.split()}
    return any(_place_matches(w, targets) for w in remainder)


def _place_matches(word: str, targets: set[str]) -> bool:
    """Exact match, or substring only for words long enough to be meaningful.

    Bidirectional substring matching on short tokens is a trap: "Remote U.S."
    tokenises to {u, s}, and "u" is a substring of "pune". Require length 4+
    before allowing partial matches.
    """
    if word in targets:
        return True
    if len(word) < 4:
        return False
    return any(len(t) >= 4 and (word in t or t in word) for t in targets)


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
