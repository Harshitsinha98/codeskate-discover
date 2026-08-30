"""Render every page in a real browser and capture it.

The sandbox will not keep a background server alive between shell calls, so the
server is started in a thread inside this one process and the browser is driven
from the same script. Seeded with realistic data — a support-engineer profile and
a mixed pool of India postings — because an empty app screenshots as an empty app
and proves nothing about the layout.

Run:  .venv/bin/python tests/shots.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

WORKDIR = Path(tempfile.mkdtemp())
os.environ["DATABASE_URL"] = f"sqlite:///{WORKDIR / 'shots.db'}"
os.environ["SECURE_COOKIES"] = "0"
os.environ["WORKER_IN_PROCESS"] = "0"
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-used")

SHOTS = Path("/projects/sandbox/.kiro/artifacts/screenshots")
SHOTS.mkdir(parents=True, exist_ok=True)
SESSION = "codeskate-ui"
PORT = 8099
BASE = f"http://127.0.0.1:{PORT}"

import uvicorn  # noqa: E402

from saas import app as app_module, auth, engine, presets, store  # noqa: E402

# --------------------------------------------------------------------------- #
# seed
# --------------------------------------------------------------------------- #
engine.create_all()

user = store.upsert_google_user("sub-demo", "harshit@example.com", "Harshit Sinha", None)
UID = user["id"]
session = auth.new_session()
store.create_session(session.token_hash, UID)
TOKEN = session.token

def _skill(name: str, level: int, years: float, source: str, detail: str) -> dict:
    return {"name": name, "level": level, "years": years,
            "evidence": [{"source": source, "detail": detail}]}


store.save_document(UID, "resume.pdf", "Service desk incident manager, 3.8 years. " * 60)
store.save_document(UID, "achievements.md", "Held SLA above 95% on a 150 ticket/day queue. " * 30)

store.save_profile(UID, {
    "candidate_name": "Harshit Sinha",
    "headline": "Service Desk Incident Manager moving into cloud support",
    "seniority": "mid",
    "total_years_experience": 3.8,
    "skills": [
        _skill("Incident management", 4, 3, "Capgemini", "Owned P1/P2 incidents end to end"),
        _skill("ITIL", 4, 3, "Capgemini", "Ran incident and problem process for the shift"),
        _skill("ServiceNow", 4, 3, "Capgemini", "Daily queue and reporting tooling"),
        _skill("SLA management", 4, 3, "Capgemini", "Held SLA above 95% for 8 quarters"),
        _skill("Escalation handling", 4, 3, "Capgemini", "Escalation point for a ~20 agent shift"),
        _skill("Team leadership", 3, 2, "Capgemini", "Shift lead for ~20 agents"),
        _skill("Network operations", 3, 1, "Vodafone", "Telecom NOC, transmission faults"),
        {"name": "Linux", "level": 1, "years": 0, "evidence": []},
        {"name": "AWS", "level": 1, "years": 0, "evidence": []},
        {"name": "Python", "level": 2, "years": 0, "evidence": []},
        {"name": "Kubernetes", "level": 1, "years": 0, "evidence": []},
    ],
    "achievements": [
        {"headline": "Held SLA above 95% across a ~150 ticket/day queue for 8 quarters",
         "metric": "95%+ SLA, ~150 tickets/day", "source": "Capgemini India",
         "skills_used": ["SLA management", "Incident management"]},
        {"headline": "Escalation point for a ~20 agent shift",
         "metric": "~20 agents", "source": "Capgemini India",
         "skills_used": ["Escalation handling", "Team leadership"]},
        {"headline": "Cut P1 mean time to acknowledge by a third",
         "metric": "-33% MTTA", "source": "Capgemini India",
         "skills_used": ["Incident management"]},
    ],
})

store.save_targets(UID, presets.targets_for(
    ["support", "sre"], years=3.8,
    cities=["Bengaluru", "Hyderabad", "Pune", "Anywhere in India"],
    min_salary_lpa=9,
    current_role="Service Desk Incident Manager / Shift Lead",
))

store.save_gap_report(UID, "Cloud Support Engineer at an MNC in Bengaluru", {
    "readiness_pct": 34,
    "verdict": "Your incident and SLA record is genuinely strong and transfers directly. "
               "What is missing is demonstrable hands-on Linux and cloud work — that is the "
               "only thing standing between you and this shortlist.",
    "gaps": [
        {"skill": "Hands-on Linux", "severity": "blocking",
         "fastest_proof": "Run a small VM, break it deliberately and write up how you diagnosed "
                          "it. Two weekends, and it gives you a real story to tell."},
        {"skill": "AWS or Azure operations", "severity": "blocking",
         "fastest_proof": "Deploy one service with monitoring and an alert that pages you. "
                          "The AWS SysOps associate cert on top makes it screenable."},
        {"skill": "Scripting", "severity": "important",
         "fastest_proof": "Automate one thing you currently do by hand in your shift and "
                          "publish it."},
    ],
})

POSTINGS = [
    ("gh-1", "Cloud Support Engineer", "Databricks", "Bengaluru, India", 78,
     "strong", ["Incident management", "SLA management", "ServiceNow", "Escalation handling"],
     ["Linux", "Spark"],
     "Your 3.8 years owning P1 incidents on a high-volume queue maps directly onto their "
     "escalation tier, and the SLA ownership is exactly what they describe. They ask for "
     "working Linux knowledge, which your profile cannot yet evidence."),
    ("gh-2", "Technical Support Engineer", "Postman", "Bengaluru, India", 74,
     "strong", ["Incident management", "ITIL", "Escalation handling"], ["API debugging"],
     "Strong overlap: they want someone who has held a queue to an SLA and can own an "
     "escalation end to end. The API debugging depth is learnable and they say so."),
    ("wd-1", "Site Reliability Engineer I", "NVIDIA", "Hyderabad, India", 61,
     "stretch", ["Incident management", "Network operations"], ["Linux", "Kubernetes", "Python"],
     "Worth a shot with a referral. Their on-call rotation and incident review process is "
     "your daily work; the Kubernetes and scripting requirements are the real gap."),
    ("lv-1", "Production Support Engineer", "Meesho", "Bengaluru, India", 66,
     "stretch", ["Incident management", "SLA management", "Team leadership"], ["SQL", "Linux"],
     "Their tier-2 production support role wants queue discipline and clear escalation "
     "judgement, both of which you have evidence for. SQL is the stated must-have."),
    ("ab-1", "Cloud Operations Engineer", "Atlan", "Bengaluru, India", 58,
     "stretch", ["Incident management", "ITIL"], ["AWS", "Terraform"],
     "The operations half is a good fit. The cloud half is not yet evidenced, and they list "
     "AWS as a hard requirement rather than a nice-to-have."),
    ("wd-2", "Technical Support Engineer", "Salesforce", "Hyderabad, India", 69,
     "stretch", ["ITIL", "ServiceNow", "Escalation handling"], ["Apex", "SOQL"],
     "Their support tier is process-heavy in exactly the way your ITIL and ServiceNow "
     "experience suits. The platform-specific skills are trained in-house."),
    ("gh-3", "Infrastructure Engineer", "Groww", "Pune, India", 42,
     "weak", ["Network operations"], ["Linux", "AWS", "Terraform", "Python"],
     "Too far from your evidence today. This is a build role rather than an operate role, "
     "and four of their five must-haves are unproven on your profile."),
    ("gh-4", "Operations Engineer", "Druva", "Pune, India", 47,
     "weak", ["Incident management"], ["Linux", "Python", "Storage"],
     "The incident half transfers but the role is mostly hands-on systems work. Revisit "
     "once you have Linux evidence."),
]

store.upsert_postings([
    {"external_id": ext, "source": "test", "company": co, "title": title,
     "location": loc, "url": f"https://example.com/{ext}",
     "description": "Responsibilities include owning incidents, holding SLAs, and "
                    "working with engineering on escalations. " * 12}
    for ext, title, co, loc, *_ in POSTINGS
])

for ext, _t, _c, _l, score, verdict, matched, missing, reasoning in POSTINGS:
    store.save_fit_score(UID, ext, {
        "score": score, "verdict": verdict, "matched_skills": matched,
        "missing_skills": missing, "reasoning": reasoning,
    })

store.add_application(UID, "gh-1")
store.set_stage(UID, "gh-1", "applied", "applied via careers page")
store.add_application(UID, "gh-2")
store.add_application(UID, "wd-2")
store.set_stage(UID, "wd-2", "screen", "recruiter call booked")

store.log_call(UID, agent="skill_graph", model="test",
               input_tokens=4200, output_tokens=900, cost_usd=0.02)
for _ in range(8):
    store.log_call(UID, agent="fit_scoring", model="test",
                   input_tokens=1800, output_tokens=260, cost_usd=0.0008)
store.log_call(UID, agent="gap_analysis", model="test",
               input_tokens=2600, output_tokens=1100, cost_usd=0.018)

print(f"seeded: {store.postings_count()} postings, {store.scored_count(UID)} scored")

# --------------------------------------------------------------------------- #
# serve
# --------------------------------------------------------------------------- #
config = uvicorn.Config(app_module.app, host="127.0.0.1", port=PORT, log_level="error")
server = uvicorn.Server(config)
threading.Thread(target=server.run, daemon=True).start()

for _ in range(60):
    if server.started:
        break
    time.sleep(0.25)
else:
    sys.exit("server did not start")
print("server up")


# --------------------------------------------------------------------------- #
# drive the browser
# --------------------------------------------------------------------------- #
def ab(*args: str, check: bool = True) -> str:
    r = subprocess.run(["agent-browser", "--session", SESSION, *args],
                       capture_output=True, text=True, timeout=120)
    if check and r.returncode != 0:
        print(f"  ! agent-browser {' '.join(args[:2])} -> {r.returncode}: "
              f"{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def shot(label: str, url: str, *, wait: float = 2.0, full: bool = False) -> Path:
    ab("open", url)
    time.sleep(wait)
    path = SHOTS / f"{label}.png"
    ab("screenshot", str(path), *(["--full"] if full else []))
    ok = path.exists() and path.stat().st_size > 0
    print(f"  {'ok  ' if ok else 'MISS'} {label}  {path.stat().st_size // 1024 if ok else 0} KB")
    return path


print("\nmarketing pages (signed out)")
shot("01-home", f"{BASE}/", full=True)
shot("02-pricing", f"{BASE}/pricing", full=True)
shot("03-signin", f"{BASE}/signin")

print("\nsigning in")
# Setting the session cookie directly is the point: the browser cannot complete a
# real Google round-trip in a sandbox, and everything being screenshotted here is
# behind that cookie.
ab("open", f"{BASE}/signin")
ab("eval", f"document.cookie = 'cs_session={TOKEN}; path=/'")

print("\nthe app")
shot("04-app-matches", f"{BASE}/app", wait=3.5, full=True)

ab("open", f"{BASE}/app")
time.sleep(3.0)
ab("eval", "showTab('saved')")
time.sleep(1.5)
ab("screenshot", str(SHOTS / "05-app-saved.png"), "--full")
print("  ok   05-app-saved")

ab("eval", "showTab('profile')")
time.sleep(1.5)
ab("screenshot", str(SHOTS / "06-app-profile.png"), "--full")
print("  ok   06-app-profile")

ab("eval", "showTab('plan')")
time.sleep(1.5)
ab("screenshot", str(SHOTS / "07-app-plan.png"), "--full")
print("  ok   07-app-plan")

ab("eval", "showTab('setup')")
time.sleep(1.5)
ab("screenshot", str(SHOTS / "08-app-setup.png"), "--full")
print("  ok   08-app-setup")

print("\ndiagnostics panel")
ab("eval", "showTab('matches')")
time.sleep(1.5)
ab("eval", "document.getElementById('btnWhy').click()")
time.sleep(2.5)
ab("screenshot", str(SHOTS / "09-app-why.png"), "--full")
print("  ok   09-app-why")

print("\nconsole errors")
errors = ab("eval", "JSON.stringify(window.__errs || [])", check=False)
print("  ", errors.strip()[:400] or "(none captured)")

print(f"\nscreenshots in {SHOTS}")
for p in sorted(SHOTS.glob("0*.png")):
    print(f"  {p.name}  {p.stat().st_size // 1024} KB")
