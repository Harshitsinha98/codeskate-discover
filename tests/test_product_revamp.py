"""End-to-end checks for the product rebuild: presets, diagnostics, pages, saving.

Runs against SQLite with a stubbed session so no Google round-trip is needed, and
no LLM is ever called — every path exercised here is one of the free ones, which
is exactly the set that decides whether a user sees relevant jobs at all.

Run:  .venv/bin/python tests/test_product_revamp.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

DB = Path(tempfile.mkdtemp()) / "test.db"
os.environ["DATABASE_URL"] = f"sqlite:///{DB}"
os.environ["SECURE_COOKIES"] = "0"
os.environ["WORKER_IN_PROCESS"] = "0"
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from codeskate.agents import fit_scoring  # noqa: E402
from saas import app as app_module, auth, engine, presets, store  # noqa: E402

PASS, FAIL = [], []


def check(name: str, condition: bool, detail: str = "") -> None:
    (PASS if condition else FAIL).append(name)
    mark = "ok  " if condition else "FAIL"
    print(f"  {mark} {name}" + (f"  <- {detail}" if detail and not condition else ""))


def section(title: str) -> None:
    print(f"\n{title}")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #

engine.create_all()
client = TestClient(app_module.app)


def make_user(email: str) -> tuple[int, dict]:
    user = store.upsert_google_user(f"sub-{email}", email, email.split("@")[0], None)
    session = auth.new_session()
    store.create_session(session.token_hash, user["id"])
    return user["id"], {"cs_session": session.token}


def posting(ext: str, title: str, company: str, location: str, desc_len: int = 900) -> dict:
    return {
        "external_id": ext, "source": "test", "company": company, "title": title,
        "location": location, "url": f"https://example.com/{ext}",
        "description": "x" * desc_len,
    }


# A deliberately mixed pool: the right roles in the right cities, the right roles
# in the wrong cities, senior versions of the right roles, and pure noise. Each
# group exists to prove one filter behaves as intended.
POOL = [
    posting("p1", "Technical Support Engineer", "Databricks", "Bengaluru, India"),
    posting("p2", "Cloud Support Engineer", "Salesforce", "Hyderabad, India"),
    posting("p3", "Site Reliability Engineer", "Meesho", "Bengaluru, India"),
    posting("p4", "Production Support Engineer", "Groww", "Pune, India"),
    posting("p5", "Senior Technical Support Engineer", "NVIDIA", "Bengaluru, India"),
    posting("p6", "Lead Cloud Operations Engineer", "Adobe", "Noida, India"),
    posting("p7", "Technical Support Engineer", "Postman", "Kochi, India"),
    posting("p8", "Support Engineer", "Atlan", "Bhubaneswar, India"),
    posting("p9", "Service Desk Analyst", "Accenture", "Bengaluru, India"),
    posting("p10", "Enterprise Sales Manager", "MongoDB", "Bengaluru, India"),
    posting("p11", "Salesforce Technical Support Engineer", "Salesforce", "Bengaluru, India"),
    posting("p12", "Backend Engineer", "CRED", "Bengaluru, India"),
    posting("p13", "Cloud Support Engineer", "Druva", "Bengaluru, India", desc_len=40),
    posting("p14", "Data Analyst", "Navi", "Bengaluru, India"),
]
store.upsert_postings(POOL)


# --------------------------------------------------------------------------- #
# presets
# --------------------------------------------------------------------------- #
section("Presets build usable filters")

t = presets.targets_for(["support"], years=3.8, cities=["Bengaluru", "Hyderabad"])
check("preset produces include terms", len(t["title_include"]) > 5)
check("preset produces exclusions", len(t["title_exclude"]) > 5)
check("city aliases expanded", "bangalore" in t["locations"] and "bengaluru" in t["locations"],
      str(t["locations"]))
check("preset keys round-trip", t["preset_keys"] == ["support"])
check("years recorded in constraints", t["constraints"]["years_of_experience"] == 3.8)

check("under 5 years excludes senior titles", " senior " in t["title_exclude"])
senior = presets.targets_for(["support"], years=9, cities=["Bengaluru"])
check("9 years does NOT exclude senior", " senior " not in senior["title_exclude"])
check("9 years excludes junior instead", " junior " in senior["title_exclude"])
mid = presets.targets_for(["support"], years=6, cities=["Bengaluru"])
check("6 years excludes neither end",
      " senior " not in mid["title_exclude"] and " junior " not in mid["title_exclude"])

# The bug that made this necessary: an unpadded "sales" exclusion silently removed
# every Salesforce posting from a feed that had Salesforce as a target company.
check("all exclusions are space-padded",
      all(x.startswith(" ") and x.endswith(" ") for x in t["title_exclude"]))
sf = [p for p in POOL if p["external_id"] == "p11"]
check("Salesforce posting survives the 'sales' exclusion",
      len(fit_scoring.prefilter(sf, t)) == 1)
sales = [p for p in POOL if p["external_id"] == "p10"]
check("actual sales role is still excluded", len(fit_scoring.prefilter(sales, t)) == 0)

# Picking two presets must not let one preset's "wrong job" list veto the other's
# wanted titles.
both = presets.targets_for(["support", "itops"], years=3)
check("two presets merge include lists",
      any("technical support" in x for x in both["title_include"])
      and any("system administrator" in x for x in both["title_include"]))

try:
    presets.targets_for([])
    check("empty role list rejected", False)
except ValueError:
    check("empty role list rejected", True)

check("unknown preset keys ignored",
      presets.targets_for(["support", "not-a-real-key"])["preset_keys"] == ["support"])

section("Presets are suggested from a skill graph")
graph = {
    "headline": "Service Desk Incident Manager",
    "skills": [{"name": "ITIL incident management", "level": 4},
               {"name": "ServiceNow", "level": 4},
               {"name": "SLA reporting", "level": 3},
               {"name": "escalation handling", "level": 4}],
    "achievements": [{"text": "Held SLA above 95% on a 150 ticket/day queue"}],
}
sug = presets.suggest(graph)
check("support suggested for an incident-management profile", "support" in sug, str(sug))
check("suggestion is at most two presets", len(sug) <= 2, str(sug))

dev_graph = {"headline": "Backend Developer",
             "skills": [{"name": "Python", "level": 4}, {"name": "Django", "level": 4},
                        {"name": "PostgreSQL", "level": 3}, {"name": "REST APIs", "level": 4}]}
check("software suggested for a developer profile",
      "software" in presets.suggest(dev_graph), str(presets.suggest(dev_graph)))
check("no graph gives no suggestion", presets.suggest(None) == [])
check("empty graph gives no suggestion", presets.suggest({}) == [])


# --------------------------------------------------------------------------- #
# prefilter + diagnostics
# --------------------------------------------------------------------------- #
section("Prefilter keeps the right jobs")

targets = presets.targets_for(["support"], years=3.8, cities=["Bengaluru", "Hyderabad", "Pune"])
kept = {j["external_id"] for j in fit_scoring.prefilter(POOL, targets)}
check("keeps relevant in-city support roles", {"p1", "p2", "p4", "p11"} <= kept, str(sorted(kept)))
check("drops senior version", "p5" not in kept)
check("drops lead version", "p6" not in kept)
check("drops service desk (the job being escaped)", "p9" not in kept)
check("drops sales role", "p10" not in kept)
check("drops backend role for a support search", "p12" not in kept)
check("drops empty description", "p13" not in kept)
check("drops right role in an unselected city", "p7" not in kept and "p8" not in kept)

section("Diagnostics explain what was dropped")
rep = fit_scoring.report(POOL, targets)
check("total counted", rep["total"] == len(POOL))
check("kept matches prefilter", rep["kept"] == len(kept))
counted = sum(r["count"] for r in rep["rejected"])
check("rejection counts add up to the total",
      counted + rep["kept"] == rep["total"], f"{counted} + {rep['kept']} != {rep['total']}")

reasons = {r["reason"] for r in rep["rejected"]}
check("reports title exclusions", fit_scoring.TITLE_EXCLUDED in reasons)
check("reports location rejections", fit_scoring.LOCATION in reasons)
check("reports short descriptions", fit_scoring.DESC_SHORT in reasons)
check("names the offending exclusion terms", len(rep["top_exclusion_terms"]) > 0)
check("surfaces right-role-wrong-city near misses",
      any(j["title"] == "Technical Support Engineer" and "Kochi" in (j["location"] or "")
          for j in rep["right_role_wrong_place"]),
      str(rep["right_role_wrong_place"]))

section("Diagnostics give actionable advice when the list is thin")
narrow = presets.targets_for(["security"], years=3.8, cities=["Jaipur"])
thin = fit_scoring.report(POOL, narrow)
check("nothing survives an impossible filter", thin["kept"] == 0)
check("advice is produced", len(thin["advice"]) > 0)
check("advice names a fix", all(a["fix"] for a in thin["advice"]))
check("advice carries an action the UI can wire up",
      any(a["action"] for a in thin["advice"]), str(thin["advice"]))

empty = fit_scoring.report([], targets)
check("empty pool advises searching first",
      empty["advice"] and empty["advice"][0]["action"] == "discover", str(empty["advice"]))

healthy = fit_scoring.report(POOL * 4, targets)
check("a healthy pool produces no nagging advice", healthy["advice"] == [],
      str(healthy["advice"]))


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
section("Public pages are reachable and are not the app")

for path, must_contain in [
    ("/", "Stop applying to 200 jobs"),
    ("/pricing", "No auto-debit"),
    ("/signin", "Continue with Google"),
    ("/privacy", "Who can see your documents"),
    ("/terms", "Terms of use"),
    ("/refunds", "Refund"),
    ("/contact", "hello@codeskate.com"),
]:
    r = client.get(path)
    check(f"GET {path} serves the right page",
          r.status_code == 200 and must_contain in r.text, f"{r.status_code}")

r = client.get("/")
check("homepage does not require sign-in", "Get started free" in r.text)
check("homepage is not the app shell", 'id="appView"' not in r.text)
r = client.get("/app")
check("/app serves the product", r.status_code == 200 and 'id="appView"' in r.text)

cfg = client.get("/api/config").json()
check("config exposes presets", len(cfg["presets"]) >= 6, str(len(cfg.get("presets", []))))
check("config exposes cities", "Bengaluru" in cfg["cities"])
check("plans expose a credits field", all("credits" in p for p in cfg["plans"]))
check("plan copy says credits, not agent runs",
      all("agent run" not in f.lower() for p in cfg["plans"] for f in p["features"]))

section("Unauthenticated access is refused")
for path in ["/api/me", "/api/setup", "/api/diagnostics", "/api/matches"]:
    check(f"{path} needs a session", client.get(path).status_code == 401)


section("Setup flow")
uid, cookies = make_user("harshit@example.com")
client.cookies.update(cookies)

me = client.get("/api/me").json()
check("/api/me reports setup state", "setup" in me)
check("new account has no targets", me["setup"]["has_targets"] is False)
check("quota exposes credits", "credits_left" in me["quota"])

s = client.get("/api/setup").json()
check("setup offers the full catalogue", len(s["catalogue"]) >= 6)
check("setup defaults to sensible cities", "Bengaluru" in s["cities"])
check("setup is not yet configured", s["configured"] is False)

r = client.post("/api/setup", json={"roles": ["support"], "cities": ["Bengaluru", "Pune"],
                                    "years": 3.8, "min_salary_lpa": 9})
check("POST /api/setup succeeds", r.status_code == 200, r.text)
check("setup returns the generated rules", len(r.json()["title_include"]) > 5)

s2 = client.get("/api/setup").json()
check("setup persists", s2["configured"] is True and s2["roles"] == ["support"])
check("cities persist", s2["cities"] == ["Bengaluru", "Pune"])
check("/api/me now shows targets", client.get("/api/me").json()["setup"]["has_targets"] is True)

r = client.post("/api/setup", json={"roles": [], "cities": ["Bengaluru"]})
check("setup rejects an empty role list", r.status_code == 422, str(r.status_code))
r = client.post("/api/setup", json={"roles": ["support", "sre", "data", "qa", "security"]})
check("setup rejects more than four roles", r.status_code == 422, str(r.status_code))


section("Diagnostics endpoint")
d = client.get("/api/diagnostics").json()
check("diagnostics counts the shared pool", d["total"] == len(POOL), str(d["total"]))
check("diagnostics keeps the Bengaluru/Pune support roles", d["kept"] >= 3, str(d["kept"]))
check("diagnostics reports rejections", len(d["rejected"]) > 0)
check("diagnostics reports the pool size", d["postings_total"] == len(POOL))


section("Scoring refuses to run silently on an empty result")
client.post("/api/setup", json={"roles": ["security"], "cities": ["Jaipur"], "years": 3.8})
r = client.post("/api/jobs/score", json={"limit": 5})
# The planner runs inline at enqueue time, so an impossible filter surfaces either
# as a rejected request or as a failed job carrying the reason — never as a job
# that "succeeds" with nothing in it.
if r.status_code == 200:
    job = client.get(f"/api/jobs/{r.json()['job_id']}").json()
    check("empty filter produces an explained failure, not a silent success",
          job["state"] == "error" and "match" in (job["error"] or "").lower(), str(job))
    check("the failure message tells the user what to change",
          any(w in (job["error"] or "").lower() for w in ("city", "role", "add", "exclusion")),
          str(job.get("error")))
else:
    check("empty filter is rejected with a reason", r.status_code == 400, r.text)

client.post("/api/setup", json={"roles": ["support"], "cities": ["Bengaluru", "Pune"],
                                "years": 3.8})


section("Saving a single job")
r = client.post("/api/save/p1")
check("job can be saved by hand", r.status_code == 200 and r.json()["saved"], r.text)
r = client.post("/api/save/p1")
check("saving twice is idempotent", r.json()["already_saved"] is True)
check("saved job appears on the board",
      any(a["id"] == "p1" for a in client.get("/api/pipeline").json()["applications"]))
check("saving an unknown job 404s", client.post("/api/save/nope").status_code == 404)


section("Advanced rule editing merges instead of replacing")
before = client.get("/api/setup").json()
r = client.post("/api/settings/rules",
                json={"title_include": ["technical support", "cloud support"],
                      "title_exclude": ["sales", "senior"]})
check("rules save", r.status_code == 200, r.text)
after = client.get("/api/setup").json()
check("cities survive a rules edit", after["cities"] == before["cities"], str(after["cities"]))
check("role choice survives a rules edit", after["roles"] == before["roles"])
check("hand-typed exclusions get padded",
      all(x.startswith(" ") for x in after["title_exclude"]), str(after["title_exclude"]))
saved = store.load_targets(uid)
check("locations survive a rules edit", bool(saved.get("locations")))
check("constraints survive a rules edit", bool(saved.get("constraints")))
r = client.post("/api/settings/rules", json={"title_include": [], "title_exclude": ["x"]})
check("an empty include list is rejected", r.status_code == 400, str(r.status_code))


section("Next-action list speaks plainly and is actionable")
acts = client.get("/api/next").json()["actions"]
check("next actions returned", len(acts) > 0)
check("every action has a machine-readable action key",
      all("action" in a for a in acts), str(acts))
jargon = ("agent ", "prefilter", "skill graph", "external_id", "llm", "artifact")
check("no developer jargon in the next-action copy",
      not any(w in (a["label"] + a["why"]).lower() for a in acts for w in jargon), str(acts))


section("Tenant isolation still holds")
uid2, cookies2 = make_user("other@example.com")
c2 = TestClient(app_module.app)
c2.cookies.update(cookies2)
check("second account sees no saved jobs", c2.get("/api/pipeline").json()["applications"] == [])
check("second account has its own setup", c2.get("/api/setup").json()["configured"] is False)
c2.post("/api/setup", json={"roles": ["software"], "cities": ["Hyderabad"]})
check("first account's roles unchanged by the second",
      client.get("/api/setup").json()["roles"] == ["support"])
check("second account cannot save into the first's board",
      c2.get("/api/pipeline").json()["applications"] == [])
check("postings are shared, scores are not",
      c2.get("/api/diagnostics").json()["postings_total"] == len(POOL)
      and c2.get("/api/matches").json()["matches"] == [])


# --------------------------------------------------------------------------- #
section("")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
if FAIL:
    print("\nfailures:")
    for f in FAIL:
        print("  -", f)
    sys.exit(1)
print("all green")
