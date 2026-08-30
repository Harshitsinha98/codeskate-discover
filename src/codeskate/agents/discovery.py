"""Agent 4 — Job Discovery.

Pulls live postings straight from companies' own ATS boards. These endpoints are
public and documented, which means: no scraping, no ToS risk, no API keys, and
no cost. Build the company list in config/companies.yaml and this becomes a
better job feed than any scraper.

Four sources, covering different parts of the market:
  greenhouse / lever / ashby  US product companies and startups — one call each
  workday                     most large MNCs, including their India requisitions

Workday matters if you are targeting MNCs: Greenhouse-style boards barely cover
them, so without it the feed skews heavily towards US-based startup roles.
"""

from __future__ import annotations

import re
import time
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


def _workday(client: httpx.Client, spec: dict) -> list[dict]:
    """Workday boards — how most large MNCs publish, including their India roles.

    Two-step, unlike the others: the list endpoint returns titles only, so the JD
    needs a second call per posting. To keep that bounded, narrow server-side with
    `search` (free, done by Workday) and cap with `max`.
    """
    tenant, wd, site = spec["tenant"], spec["wd"], spec["site"]
    cap = int(spec.get("max", 40))
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    searches = spec.get("search", "")
    if isinstance(searches, str):
        searches = [searches]

    postings: list[dict] = []
    seen_paths: set[str] = set()

    for term in searches:
        offset = 0
        collected = 0
        while collected < cap:
            r = client.post(
                f"{base}/jobs",
                headers=headers,
                json={"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": term},
            )
            r.raise_for_status()
            page = r.json().get("jobPostings", [])
            if not page:
                break
            for p in page:
                if p.get("externalPath") not in seen_paths:
                    seen_paths.add(p.get("externalPath"))
                    postings.append(p)
            collected += len(page)
            offset += 20

    out = []
    for p in postings:
        path = p.get("externalPath")
        if not path:
            continue
        job_id = (p.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]
        try:
            d = client.get(f"{base}{path}", headers={"Accept": "application/json"})
            d.raise_for_status()
            info = d.json().get("jobPostingInfo", {})
        except Exception:  # noqa: BLE001 - skip one bad posting, keep the rest
            continue

        out.append(
            {
                "external_id": f"wd:{tenant}:{job_id}",
                "source": "workday",
                "company": tenant,
                "title": info.get("title") or p.get("title", ""),
                "location": info.get("location") or p.get("locationsText"),
                "url": info.get("externalUrl", ""),
                "description": _clean(info.get("jobDescription")),
            }
        )
        time.sleep(0.15)  # be a polite client; these are free endpoints

    return out


SLUG_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}
ALL_SOURCES = set(SLUG_FETCHERS) | {"workday"}


def load_company_config() -> dict[str, list]:
    path = CONFIG_DIR / "companies.yaml"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items() if k in ALL_SOURCES}


def fetch_all(config: dict[str, list] | None = None) -> tuple[list[dict], list[str]]:
    """Return (jobs, errors). One dead board never aborts the run."""
    config = config or load_company_config()
    jobs: list[dict] = []
    errors: list[str] = []

    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        for ats, entries in config.items():
            for entry in entries:
                label = entry["tenant"] if isinstance(entry, dict) else entry
                try:
                    if ats == "workday":
                        found = _workday(client, entry)
                    else:
                        found = SLUG_FETCHERS[ats](client, entry)
                    jobs.extend(found)
                    print(f"  {ats:<11} {label:<24} {len(found):>4} jobs")
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{ats}/{label}: {type(e).__name__}")

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
