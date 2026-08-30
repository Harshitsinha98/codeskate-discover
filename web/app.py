"""Web interface — the layer a user actually sees.

Wraps the existing agents rather than reimplementing them: every endpoint here
calls the same functions the CLI calls. The agents stay the engine; this is the
steering wheel.

Long operations (discovery, scoring, profile extraction) run on background
threads with a pollable task registry, because they take minutes and a browser
request cannot wait that long. That constraint is also why this cannot be
deployed to a serverless platform as-is.
"""

from __future__ import annotations

import json
import threading
import traceback
import uuid
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from codeskate import db
from codeskate.agents import (
    career_manager,
    discovery,
    fit_scoring,
    gap_analysis,
    interview_prep,
    intake,
    outreach,
    pipeline,
    resume_tailoring,
    skill_graph,
)
from codeskate.llm import LLM, SpendLimitExceeded
from codeskate.models import SkillGraph, TailoredResume
from codeskate.settings import INBOX_DIR, load_settings

STATIC = Path(__file__).parent / "static"

app = FastAPI(title="CodeSkate", docs_url="/api/docs")

# ---------------------------------------------------------------------------
# task registry — long jobs run on threads, the browser polls for progress
# ---------------------------------------------------------------------------

TASKS: dict[str, dict[str, Any]] = {}
_LOCK = threading.Lock()


def _new_task(label: str) -> str:
    task_id = uuid.uuid4().hex[:12]
    with _LOCK:
        TASKS[task_id] = {"label": label, "state": "running", "log": [], "result": None, "error": None}
    return task_id


def _log(task_id: str, line: str) -> None:
    with _LOCK:
        TASKS[task_id]["log"].append(line)


def _finish(task_id: str, result: Any = None, error: str | None = None) -> None:
    with _LOCK:
        TASKS[task_id]["state"] = "error" if error else "done"
        TASKS[task_id]["result"] = result
        TASKS[task_id]["error"] = error


def _run(task_id: str, fn) -> None:
    def wrapper() -> None:
        try:
            _finish(task_id, result=fn(task_id))
        except SpendLimitExceeded as e:
            _finish(task_id, error=f"Spend limit reached: {e}")
        except SystemExit as e:
            _finish(task_id, error=str(e))
        except Exception as e:  # noqa: BLE001
            _finish(task_id, error=f"{type(e).__name__}: {e}")
            traceback.print_exc()

    threading.Thread(target=wrapper, daemon=True).start()


def _llm():
    """Fresh connection per caller — SQLite objects are not thread-safe."""
    settings = load_settings()
    conn = db.connect()
    return LLM(settings, conn), conn


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------


@app.get("/api/state")
def state() -> dict:
    conn = db.connect()
    settings = load_settings()
    raw = db.load_profile(conn)
    graph = SkillGraph.model_validate(raw) if raw else None

    return {
        "provider": settings.provider,
        "models": {"cheap": settings.model_cheap, "smart": settings.model_smart},
        "api_key_set": bool(settings.api_key),
        "spend": {
            "used": round(db.total_spend(conn), 4),
            "limit": settings.spend_limit_usd,
        },
        "inbox_files": [p.name for p in INBOX_DIR.iterdir() if p.is_file() and not p.name.startswith(".")]
        if INBOX_DIR.exists()
        else [],
        "profile": None
        if graph is None
        else {
            "name": graph.candidate_name,
            "headline": graph.headline,
            "seniority": graph.seniority,
            "years": graph.total_years_experience,
            "skills_total": len(graph.skills),
            "skills_proven": len(graph.claimable_skills()),
            "achievements": len(graph.achievements),
            "skills": [
                {"name": s.name, "level": s.level, "years": s.years, "proven": bool(s.evidence)}
                for s in sorted(graph.skills, key=lambda s: (-s.level, s.name))
            ],
        },
        "jobs_total": conn.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"],
        "jobs_scored": conn.execute("SELECT COUNT(*) c FROM fit_scores").fetchone()["c"],
        "funnel": {k: v for k, v in pipeline.funnel(conn).items() if v},
        "gap_report": db.latest_gap_report(conn),
        "next_actions": [
            {"label": a.label, "why": a.why, "command": a.command}
            for a in career_manager.next_actions(conn, limit=6)
        ],
    }


@app.get("/api/task/{task_id}")
def task_status(task_id: str) -> dict:
    with _LOCK:
        task = TASKS.get(task_id)
    if not task:
        raise HTTPException(404, "unknown task")
    return task


@app.get("/api/spend")
def spend() -> dict:
    conn = db.connect()
    rows = conn.execute(
        """SELECT agent, model, COUNT(*) calls, SUM(input_tokens) tin,
                  SUM(output_tokens) tout, SUM(cost_usd) cost
           FROM llm_calls GROUP BY agent, model ORDER BY cost DESC"""
    ).fetchall()
    return {
        "total": round(db.total_spend(conn), 4),
        "limit": load_settings().spend_limit_usd,
        "by_agent": [dict(r) for r in rows],
    }


# ---------------------------------------------------------------------------
# Pod 1 — intake and profiling
# ---------------------------------------------------------------------------


@app.post("/api/upload")
async def upload(files: list[UploadFile] = File(...)) -> dict:
    INBOX_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    for f in files:
        name = Path(f.filename or "upload").name
        if not name.lower().endswith((".pdf", ".md", ".txt", ".markdown")):
            continue
        (INBOX_DIR / name).write_bytes(await f.read())
        saved.append(name)
    if not saved:
        raise HTTPException(400, "Only .pdf, .md and .txt files are accepted")
    return {"saved": saved}


@app.delete("/api/upload/{name}")
def delete_upload(name: str) -> dict:
    path = INBOX_DIR / Path(name).name
    if path.exists():
        path.unlink()
    return {"deleted": name}


@app.post("/api/profile")
def build_profile(bg: BackgroundTasks) -> dict:
    task_id = _new_task("Building your skill graph")

    def job(tid: str):
        _log(tid, "Agent 1 — reading your documents")
        raw, files = intake.collect()
        _log(tid, f"read {len(files)} file(s): {', '.join(files)}")
        llm, conn = _llm()
        _log(tid, f"Agent 2 — extracting skills with {llm.s.model_smart}")
        graph = skill_graph.build(llm, raw)
        db.save_profile(conn, graph.model_dump_json())
        proven = len(graph.claimable_skills())
        _log(tid, f"done — {len(graph.skills)} skills, {proven} evidence-backed")
        return {"skills": len(graph.skills), "proven": proven, "achievements": len(graph.achievements)}

    _run(task_id, job)
    return {"task_id": task_id}


class GapRequest(BaseModel):
    role: str


@app.post("/api/gaps")
def gaps(req: GapRequest) -> dict:
    task_id = _new_task(f"Analysing gaps for {req.role}")

    def job(tid: str):
        llm, conn = _llm()
        raw = db.load_profile(conn)
        if not raw:
            raise SystemExit("Build your profile first")
        _log(tid, "Agent 3 — comparing your evidence against the role")
        report = gap_analysis.run(llm, SkillGraph.model_validate(raw), req.role)
        db.save_gap_report(conn, req.role, report.model_dump_json())
        _log(tid, f"readiness {report.readiness_pct}%")
        return report.model_dump()

    _run(task_id, job)
    return {"task_id": task_id}


# ---------------------------------------------------------------------------
# Pod 2 — market
# ---------------------------------------------------------------------------


@app.post("/api/discover")
def discover() -> dict:
    task_id = _new_task("Pulling live jobs")

    def job(tid: str):
        conn = db.connect()
        _log(tid, "Agent 4 — querying public ATS boards (free)")
        jobs, errors = discovery.fetch_all()
        jobs = discovery.dedupe(jobs)
        new = db.upsert_jobs(conn, jobs) if jobs else 0
        _log(tid, f"fetched {len(jobs)}, {new} new")
        for e in errors:
            _log(tid, f"unreachable: {e}")
        return {"fetched": len(jobs), "new": new, "errors": errors}

    _run(task_id, job)
    return {"task_id": task_id}


class ScoreRequest(BaseModel):
    limit: int = 25


@app.post("/api/score")
def score(req: ScoreRequest) -> dict:
    task_id = _new_task(f"Scoring up to {req.limit} jobs")

    def job(tid: str):
        llm, conn = _llm()
        raw = db.load_profile(conn)
        if not raw:
            raise SystemExit("Build your profile first")

        graph = SkillGraph.model_validate(raw)
        targets = fit_scoring.load_targets()
        brief = skill_graph.profile_brief(graph)
        constraints = fit_scoring.constraints_text(targets)

        candidates = db.unscored_jobs(conn, 5000)
        kept = fit_scoring.prefilter(candidates, targets)
        _log(tid, f"prefilter (free): {len(candidates)} -> {len(kept)}")
        kept = kept[:req.limit]
        if not kept:
            _log(tid, "nothing survived the filters — loosen config/targets.yaml")
            return {"scored": 0}

        done = 0
        for j in kept:
            result = fit_scoring.score_job(llm, brief, j, constraints)
            db.save_fit_score(conn, j["external_id"], result.model_dump())
            done += 1
            _log(tid, f"{result.score:>3} [{result.verdict}] {j['company']} — {j['title'][:44]}")
        return {"scored": done}

    _run(task_id, job)
    return {"task_id": task_id}


@app.get("/api/matches")
def matches(limit: int = 30, min_score: int = 0) -> dict:
    conn = db.connect()
    rows = db.top_matches(conn, limit, min_score)
    return {
        "matches": [
            {
                "id": r["external_id"],
                "company": r["company"],
                "title": r["title"],
                "location": r["location"],
                "url": r["url"],
                "score": r["score"],
                "verdict": r["verdict"],
                "matched": json.loads(r["matched_skills"] or "[]"),
                "missing": json.loads(r["missing_skills"] or "[]"),
                "reasoning": r["reasoning"],
                "stage": r["stage"],
            }
            for r in rows
        ]
    }


# ---------------------------------------------------------------------------
# Pod 3 — execution
# ---------------------------------------------------------------------------


class ShortlistRequest(BaseModel):
    min_score: int = 70
    limit: int = 10


@app.post("/api/shortlist")
def shortlist(req: ShortlistRequest) -> dict:
    conn = db.connect()
    rows = conn.execute(
        """SELECT f.external_id, f.score FROM fit_scores f
           LEFT JOIN applications a ON a.external_id = f.external_id
           WHERE f.score >= ? AND a.external_id IS NULL
           ORDER BY f.score DESC LIMIT ?""",
        (req.min_score, req.limit),
    ).fetchall()
    for r in rows:
        db.add_application(conn, r["external_id"])
        db.record_outcome(conn, "shortlisted", f"score {r['score']}", r["external_id"])
    return {"added": len(rows)}


@app.post("/api/tailor/{job_id}")
def tailor(job_id: str) -> dict:
    task_id = _new_task("Tailoring your resume")

    def job(tid: str):
        llm, conn = _llm()
        raw = db.load_profile(conn)
        if not raw:
            raise SystemExit("Build your profile first")
        graph = SkillGraph.model_validate(raw)
        j = db.job_by_id(conn, job_id)
        if not j:
            raise SystemExit("job not found")
        if not graph.achievements:
            raise SystemExit(
                "Your profile has no achievements. This agent can only rearrange facts you "
                "gave it — add a brag document with quantified accomplishments."
            )

        db.add_application(conn, job_id)
        _log(tid, f"Agent 8 — writing for {j['title']} @ {j['company']}")
        resume = resume_tailoring.run(llm, graph, j["title"], j["company"], j["description"] or "")
        violations = resume_tailoring.verify(resume, graph)
        db.save_artifact(conn, "resume", resume.model_dump_json(), job_id)
        current = db.get_application(conn, job_id)["stage"]
        if pipeline.can_move(current, "tailored"):
            pipeline.advance(conn, job_id, "tailored", "resume generated")

        _log(tid, f"fabrication check: {len(violations)} violation(s)")
        return {"resume": resume.model_dump(), "violations": violations}

    _run(task_id, job)
    return {"task_id": task_id}


@app.post("/api/outreach/{job_id}")
def outreach_ep(job_id: str) -> dict:
    task_id = _new_task("Drafting outreach")

    def job(tid: str):
        llm, conn = _llm()
        graph = SkillGraph.model_validate(db.load_profile(conn) or {})
        j = db.job_by_id(conn, job_id)
        stored = db.latest_artifact(conn, "resume", job_id)
        if not stored:
            raise SystemExit("Tailor the resume first")
        _log(tid, "Agent 9 — cover letter, recruiter DM, hiring-manager email")
        pack = outreach.run(
            llm, graph, TailoredResume.model_validate(stored),
            j["title"], j["company"], j["description"] or "",
        )
        db.save_artifact(conn, "outreach", pack.model_dump_json(), job_id)
        return pack.model_dump()

    _run(task_id, job)
    return {"task_id": task_id}


@app.post("/api/prep/{job_id}")
def prep(job_id: str) -> dict:
    task_id = _new_task("Building interview prep")

    def job(tid: str):
        llm, conn = _llm()
        graph = SkillGraph.model_validate(db.load_profile(conn) or {})
        j = db.job_by_id(conn, job_id)
        _log(tid, "Agent 12 — questions, STAR stories, weak spots")
        brief = interview_prep.run(
            llm, graph, j["title"], j["company"], j["description"] or "", None, "screen"
        )
        db.save_artifact(conn, "prep_brief", brief.model_dump_json(), job_id)
        return brief.model_dump()

    _run(task_id, job)
    return {"task_id": task_id}


@app.get("/api/artifact/{kind}/{job_id}")
def artifact(kind: str, job_id: str) -> dict:
    conn = db.connect()
    payload = db.latest_artifact(conn, kind, job_id)
    if payload is None:
        raise HTTPException(404, f"no {kind} generated for this job yet")
    return {"kind": kind, "job_id": job_id, "payload": payload}


# ---------------------------------------------------------------------------
# Pod 5 — pipeline
# ---------------------------------------------------------------------------


@app.get("/api/pipeline")
def pipeline_view() -> dict:
    conn = db.connect()
    return {
        "stages": pipeline.STAGES,
        "funnel": pipeline.funnel(conn),
        "rates": pipeline.conversion_rates(conn),
        "applications": [
            {
                "id": r["external_id"], "company": r["company"], "title": r["title"],
                "stage": r["stage"], "score": r["score"], "days": r["days_in_stage"],
                "url": r["url"],
            }
            for r in db.list_applications(conn)
        ],
        "needs_followup": [r["external_id"] for r in pipeline.needs_followup(conn)],
        "ghosts": [r["external_id"] for r in pipeline.ghost_candidates(conn)],
    }


class StageRequest(BaseModel):
    to: str
    note: str = ""


@app.post("/api/stage/{job_id}")
def set_stage(job_id: str, req: StageRequest) -> dict:
    conn = db.connect()
    try:
        pipeline.advance(conn, job_id, req.to, req.note)
    except pipeline.InvalidTransition as e:
        raise HTTPException(400, str(e)) from e
    return {"id": job_id, "stage": req.to}


# ---------------------------------------------------------------------------
# static frontend
# ---------------------------------------------------------------------------


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


@app.get("/static/brag-template.md")
def brag_template() -> FileResponse:
    """Served from docs/ rather than duplicated into static/."""
    path = Path(__file__).parents[1] / "docs" / "brag-template.md"
    return FileResponse(path, media_type="text/markdown", filename="brag-template.md")


app.mount("/static", StaticFiles(directory=STATIC), name="static")

# Note: no app-level handler for SystemExit. It derives from BaseException, not
# Exception, so Starlette refuses to register one. The agent modules raise it for
# missing config, which only happens inside background tasks — and _run() already
# converts it into a task error the UI can display.
