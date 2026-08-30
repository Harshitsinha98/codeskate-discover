"""Agent 4 — Job Discovery.

Pulls live postings straight from companies' own ATS boards. These endpoints are
public and documented, which means: no scraping, no ToS risk, no API keys, and
no cost. Build the company list in config/companies.yaml and this becomes a
better job feed than any scraper.
"""

from __future__ import annotations

import re
from typing import Iterable

import httpx
import yaml

from ..settings import CONFIG_DIR

TIMEOUT = httpx.Timeout(30.0)
TAG_RE = re.compile(r"<[^>]+>")


def _clean(html: str | None, limit: int = 6000) -> str:
    if not html:
        return ""
    text = TAG_RE.sub(" ", html)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "), ("&#39;", "'"), ("&quot;", '"')):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _greenhouse(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(
            {
                "external_id": f"gh:{slug}:{j['id']}",
                "source": "greenhouse",
                "company": slug,
                "title": j.get("title", ""),
                "location": (j.get("location") or {}).get("name"),
                "url": j.get("absolute_url", ""),
                "description": _clean(j.get("content")),
            }
        )
    return out


def _lever(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.lever.co/v0/postings/{slug}?mode=json")
    r.raise_for_status()
    out = []
    for j in r.json():
        out.append(
            {
                "external_id": f"lv:{slug}:{j['id']}",
                "source": "lever",
                "company": slug,
                "title": j.get("text", ""),
                "location": (j.get("categories") or {}).get("location"),
                "url": j.get("hostedUrl", ""),
                "description": _clean(j.get("descriptionPlain") or j.get("description")),
            }
        )
    return out


def _ashby(client: httpx.Client, slug: str) -> list[dict]:
    r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true")
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        out.append(
            {
                "external_id": f"ab:{slug}:{j.get('id')}",
                "source": "ashby",
                "company": slug,
                "title": j.get("title", ""),
                "location": j.get("location"),
                "url": j.get("jobUrl", ""),
                "description": _clean(j.get("descriptionPlain") or j.get("descriptionHtml")),
            }
        )
    return out


FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}


def load_company_config() -> dict[str, list[str]]:
    path = CONFIG_DIR / "companies.yaml"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items() if k in FETCHERS}


def fetch_all(config: dict[str, list[str]] | None = None) -> tuple[list[dict], list[str]]:
    """Return (jobs, errors). One dead board never aborts the run."""
    config = config or load_company_config()
    jobs: list[dict] = []
    errors: list[str] = []

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for ats, slugs in config.items():
            fetcher = FETCHERS[ats]
            for slug in slugs:
                try:
                    found = fetcher(client, slug)
                    jobs.extend(found)
                    print(f"  {ats:<11} {slug:<24} {len(found):>4} jobs")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{ats}/{slug}: {type(e).__name__}")

    return jobs, errors


def dedupe(jobs: Iterable[dict]) -> list[dict]:
    seen: set[str] = set()
    out = []
    for j in jobs:
        if j["external_id"] in seen or not j.get("url"):
            continue
        seen.add(j["external_id"])
        out.append(j)
    return out
