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
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterable

import httpx
import yaml

from ..settings import CONFIG_DIR

TIMEOUT = httpx.Timeout(30.0)
TAG_RE = re.compile(r"<[^>]+>")

# Two levels of concurrency, both deliberately modest. These hit other people's
# free endpoints, so the aim is "fast enough" rather than maximum throughput.
# Worst case in flight is BOARD_CONCURRENCY * WORKDAY_CONCURRENCY requests, and
# each board's requests go to a different host.
BOARD_CONCURRENCY = 6
WORKDAY_CONCURRENCY = 6


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

    # Detail requests run concurrently. Sequentially, with a courtesy sleep between
    # each, ~460 postings took several minutes and looked like a hang. A small
    # worker pool keeps the same politeness (a handful of in-flight requests, not a
    # flood) while cutting wall time by roughly an order of magnitude.
    def fetch_detail(p: dict) -> dict | None:
        path = p.get("externalPath")
        if not path:
            return None
        job_id = (p.get("bulletFields") or [path.rsplit("_", 1)[-1]])[0]

        for attempt in range(3):
            try:
                d = client.get(f"{base}{path}", headers={"Accept": "application/json"})
                if d.status_code == 429:
                    time.sleep(1.5 * (attempt + 1))  # backoff and retry
                    continue
                d.raise_for_status()
                info = d.json().get("jobPostingInfo", {})
            except Exception:  # noqa: BLE001 - one bad posting must not kill the board
                return None

            return {
                "external_id": f"wd:{tenant}:{job_id}",
                "source": "workday",
                "company": tenant,
                "title": info.get("title") or p.get("title", ""),
                "location": info.get("location") or p.get("locationsText"),
                "url": info.get("externalUrl", ""),
                "description": _clean(info.get("jobDescription")),
            }
        return None

    out: list[dict] = []
    with ThreadPoolExecutor(max_workers=WORKDAY_CONCURRENCY) as pool:
        for result in pool.map(fetch_detail, postings):
            if result:
                out.append(result)

    return out


SLUG_FETCHERS = {"greenhouse": _greenhouse, "lever": _lever, "ashby": _ashby}
ALL_SOURCES = set(SLUG_FETCHERS) | {"workday"}


def load_company_config() -> dict[str, list]:
    path = CONFIG_DIR / "companies.yaml"
    if not path.exists():
        raise SystemExit(f"missing {path}")
    data = yaml.safe_load(path.read_text()) or {}
    return {k: list(v or []) for k, v in data.items() if k in ALL_SOURCES}


def fetch_all(
    config: dict[str, list] | None = None,
    on_board: Callable[[str, str, int, float], None] | None = None,
) -> tuple[list[dict], list[str]]:
    """Return (jobs, errors). One dead board never aborts the run.

    `on_board(ats, label, count, seconds)` is called after each board so a caller
    can render live progress. The default printer flushes explicitly: stdout is
    block-buffered when piped, so without it a multi-minute run shows nothing at
    all until it finishes and looks like a hang.
    """
    config = config or load_company_config()
    jobs: list[dict] = []
    errors: list[str] = []

    lock = threading.Lock()

    def report(ats: str, label: str, count: int, seconds: float) -> None:
        if on_board:
            on_board(ats, label, count, seconds)
        else:
            print(f"  {ats:<11} {label:<24} {count:>4} jobs  {seconds:5.1f}s", flush=True)

    boards = [(ats, entry) for ats, entries in config.items() for entry in entries]

    def fetch_board(item: tuple[str, object]) -> list[dict]:
        ats, entry = item
        label = entry["tenant"] if isinstance(entry, dict) else entry
        started = time.monotonic()
        try:
            if ats == "workday":
                found = _workday(client, entry)
            else:
                found = SLUG_FETCHERS[ats](client, entry)
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"{ats}/{label}: {type(e).__name__}")
            report(ats, label, 0, time.monotonic() - started)
            return []
        report(ats, label, len(found), time.monotonic() - started)
        return found

    # Boards are fetched concurrently as well as postings within a board. Workday's
    # list endpoint is slow per call regardless of result size — one board returning
    # six postings still took ~18s — so the wall time was dominated by waiting on
    # other people's servers in sequence rather than by any work of ours. Running
    # boards in parallel makes total time track the slowest board instead of the sum.
    with httpx.Client(timeout=TIMEOUT, follow_redirects=True) as client:
        with ThreadPoolExecutor(max_workers=BOARD_CONCURRENCY) as pool:
            for found in pool.map(fetch_board, boards):
                jobs.extend(found)

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


# Cities and markers that identify an Indian posting. Kept broad on purpose:
# a company writes locations many ways ("Bengaluru", "Bangalore, KA", "IN").
INDIA_MARKERS = (
    "india", " in ", "bengaluru", "bangalore", "hyderabad", "pune", "chennai",
    "mumbai", "gurgaon", "gurugram", "noida", "delhi", "kolkata", "ahmedabad",
    "coimbatore", "kochi", "cochin", "trivandrum", "thiruvananthapuram", "mohali",
    "chandigarh", "jaipur", "indore", "nagpur", "vadodara", "visakhapatnam",
    "karnataka", "telangana", "maharashtra", "tamil nadu", "haryana",
)

# Places that look Indian by substring but are not, so they are not misread.
_NON_INDIA_TRAP = ("indiana", "indianapolis")


def is_india(job: dict) -> bool:
    """True if the posting is in India, or is remote with no non-India region.

    Location strings are inconsistent across boards, so this matches broadly on
    Indian cities, states and country markers. A remote role with no region named
    is kept — it is open to India — while "Remote, US" is dropped.
    """
    loc = (job.get("location") or "").lower()
    title = (job.get("title") or "").lower()

    for trap in _NON_INDIA_TRAP:
        loc = loc.replace(trap, "")

    if any(m in loc for m in INDIA_MARKERS):
        return True

    # A purely remote posting with no country attached is open to India.
    if not loc.strip() or "remote" in loc or "remote" in title:
        non_india = ("usa", "united states", " us ", "u.s", "canada", "emea",
                     "europe", "uk", "united kingdom", "singapore", "australia",
                     "germany", "poland", "brazil", "philippines", "japan", "china")
        if not any(r in loc for r in non_india):
            return True

    return False


def india_only(jobs: Iterable[dict]) -> list[dict]:
    return [j for j in jobs if is_india(j)]
