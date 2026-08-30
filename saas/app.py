"""CodeSkate SaaS — multi-tenant API.

Every per-user endpoint resolves the caller from a session cookie and passes that
user_id into the store, which has no unscoped accessors for tenant data. The agents
are imported and used exactly as the CLI uses them.

Work is queued rather than run inline. After enqueueing, the worker is nudged for a
short budget so single-unit jobs usually complete before the response returns and
feel synchronous; multi-unit jobs like discovery are carried on by the cron worker.
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from codeskate.agents import career_manager, pipeline
from codeskate.models import SkillGraph

from . import admin, auth, crypto, handlers, mailer, queue, store  # handlers registers kinds
from .engine import create_all
from .runtime import DEFAULT_MODELS, MissingKey

STATIC = Path(__file__).parent / "static"
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "1") != "0"
INLINE_WORKER_BUDGET = float(os.getenv("INLINE_WORKER_BUDGET", "12"))

app = FastAPI(title="CodeSkate", docs_url=None, redoc_url=None)

_schema_ready = False


@app.middleware("http")
async def ensure_schema(request: Request, call_next):  # noqa: ANN001, ANN201
    """Create tables on first request.

    Serverless has no startup hook that reliably runs once, and a migration tool
    is overkill for a schema this size. create_all is idempotent.
    """
    global _schema_ready
    if not _schema_ready:
        create_all()
        _schema_ready = True
    return await call_next(request)


# --------------------------------------------------------------------------- #
# auth plumbing
# --------------------------------------------------------------------------- #


def current_user(cs_session: str | None = Cookie(default=None)) -> dict:
    if not cs_session:
        raise HTTPException(401, "Sign in to continue")
    user = store.session_user(auth.hash_token(cs_session))
    if not user:
        raise HTTPException(401, "Your session has expired — sign in again")
    return user


class Credentials(BaseModel):
    email: str
    password: str


@app.post("/api/auth/signup")
def signup(body: Credentials, response: Response) -> dict:
    try:
        email = auth.validate_email(body.email)
        password = auth.validate_password(body.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e

    if store.user_by_email(email):
        raise HTTPException(400, "An account with that email already exists")

    user_id = store.create_user(email, auth.hash_password(password))
    session = auth.new_session()
    store.create_session(session.token_hash, user_id)
    response.set_cookie(value=session.token, **auth.cookie_kwargs(SECURE_COOKIES))
    return {"email": email}


@app.post("/api/auth/login")
def login(body: Credentials, response: Response) -> dict:
    user = store.user_by_email(body.email or "")
    # Same message either way — revealing which emails exist helps nobody but an
    # attacker enumerating accounts.
    if not user or not auth.verify_password(body.password or "", user["password_hash"]):
        raise HTTPException(401, "Email or password is incorrect")
    if not user["is_active"]:
        raise HTTPException(403, "This account is disabled")

    session = auth.new_session()
    store.create_session(session.token_hash, user["id"])
    store.touch_login(user["id"])
    response.set_cookie(value=session.token, **auth.cookie_kwargs(SECURE_COOKIES))
    return {"email": user["email"]}


@app.post("/api/auth/logout")
def logout(response: Response, cs_session: str | None = Cookie(default=None)) -> dict:
    if cs_session:
        store.delete_session(auth.hash_token(cs_session))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


# --------------------------------------------------------------------------- #
# password reset
# --------------------------------------------------------------------------- #


class ForgotBody(BaseModel):
    email: str


@app.post("/api/auth/forgot")
def forgot_password(body: ForgotBody, request: Request) -> dict:
    """Always reports success.

    Reporting whether an address exists turns this endpoint into an account
    enumeration tool, so the response is identical either way and the work happens
    only when there is really an account.
    """
    user = store.user_by_email(body.email or "")
    if user and user["is_active"]:
        session = auth.new_session()  # same generator: random token, hash stored
        store.create_password_reset(session.token_hash, user["id"])
        base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")
        mailer.send_password_reset(user["email"], f"{base}/reset?token={session.token}")

    return {
        "ok": True,
        "detail": "If that email has an account, a reset link is on its way.",
        # Tells the UI to warn the operator, without leaking anything about the
        # account: with no provider configured the link only reaches the log.
        "mail_configured": mailer.configured(),
    }


class ResetBody(BaseModel):
    token: str
    password: str


@app.post("/api/auth/reset")
def reset_password(body: ResetBody) -> dict:
    try:
        password = auth.validate_password(body.password)
    except auth.AuthError as e:
        raise HTTPException(400, str(e)) from e

    user_id = store.consume_password_reset(auth.hash_token(body.token or ""))
    if user_id is None:
        raise HTTPException(400, "That reset link is invalid, already used, or expired")

    store.set_password(user_id, auth.hash_password(password))
    return {"ok": True, "detail": "Password changed. Sign in with your new password."}


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


class KeyBody(BaseModel):
    provider: str = Field(pattern="^(openai|anthropic)$")
    api_key: str
    model_cheap: str | None = None
    model_smart: str | None = None


@app.post("/api/settings/key")
def save_key(body: KeyBody, user: dict = Depends(current_user)) -> dict:
    key = body.api_key.strip()
    if len(key) < 20:
        raise HTTPException(400, "That does not look like a valid API key")

    cheap_default, smart_default = DEFAULT_MODELS[body.provider]
    store.save_user_key(
        user["id"], body.provider, crypto.encrypt(key), crypto.hint(key),
        (body.model_cheap or cheap_default).strip(),
        (body.model_smart or smart_default).strip(),
    )
    return {"provider": body.provider, "hint": crypto.hint(key)}


@app.delete("/api/settings/key")
def remove_key(user: dict = Depends(current_user)) -> dict:
    store.delete_user_key(user["id"])
    return {"ok": True}


class LimitBody(BaseModel):
    spend_limit_usd: float = Field(ge=0, le=1000)


@app.post("/api/settings/limit")
def set_limit(body: LimitBody, user: dict = Depends(current_user)) -> dict:
    store.set_spend_limit(user["id"], body.spend_limit_usd)
    return {"spend_limit_usd": body.spend_limit_usd}


class TargetsBody(BaseModel):
    config: dict


@app.post("/api/settings/targets")
def save_targets(body: TargetsBody, user: dict = Depends(current_user)) -> dict:
    store.save_targets(user["id"], body.config)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    user_id = user["id"]
    key = store.get_user_key(user_id)
    raw = store.load_profile(user_id)
    graph = SkillGraph.model_validate(raw) if raw else None
    counts = store.stage_counts(user_id)

    return {
        "email": user["email"],
        "is_admin": admin.is_admin(user["email"]),
        "mail_configured": mailer.configured(),
        "spend": {"used": round(store.total_spend(user_id), 4),
                  "limit": float(user["spend_limit_usd"])},
        "key": None if not key else {"provider": key["provider"], "hint": key["key_hint"],
                                     "model_cheap": key["model_cheap"],
                                     "model_smart": key["model_smart"]},
        "documents": store.list_documents(user_id),
        "profile": None if graph is None else {
            "name": graph.candidate_name, "headline": graph.headline,
            "seniority": graph.seniority, "years": graph.total_years_experience,
            "skills_total": len(graph.skills),
            "skills_proven": len(graph.claimable_skills()),
            "achievements": len(graph.achievements),
            "skills": [
                {"name": s.name, "level": s.level, "years": s.years, "proven": bool(s.evidence)}
                for s in sorted(graph.skills, key=lambda s: (-s.level, s.name))
            ],
        },
        "postings_total": store.postings_count(),
        "scored_total": store.scored_count(user_id),
        "funnel": {k: v for k, v in counts.items() if v},
        "gap_report": store.latest_gap_report(user_id),
        "strong_unpursued": store.unpursued_strong_count(user_id),
        "active_jobs": [
            {"id": j["id"], "kind": j["kind"], "label": j["label"], "state": j["state"],
             "units_done": j["units_done"], "units_total": j["units_total"]}
            for j in queue.active_jobs(user_id)
        ],
    }


# --------------------------------------------------------------------------- #
# documents
# --------------------------------------------------------------------------- #

ALLOWED_SUFFIXES = (".pdf", ".md", ".txt", ".markdown")
MAX_UPLOAD_BYTES = 2 * 1024 * 1024


@app.post("/api/documents")
async def upload(files: list[UploadFile] = File(...), user: dict = Depends(current_user)) -> dict:
    saved = []
    for f in files:
        name = Path(f.filename or "upload").name
        if not name.lower().endswith(ALLOWED_SUFFIXES):
            continue
        blob = await f.read()
        if len(blob) > MAX_UPLOAD_BYTES:
            raise HTTPException(400, f"{name} is larger than 2MB")

        if name.lower().endswith(".pdf"):
            import io

            from pypdf import PdfReader

            try:
                reader = PdfReader(io.BytesIO(blob))
                text = "\n".join((p.extract_text() or "") for p in reader.pages)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(400, f"Could not read {name}: {e}") from e
        else:
            text = blob.decode("utf-8", "replace")

        if not text.strip():
            raise HTTPException(400, f"{name} appears to be empty or is a scanned image")

        # Extract once at upload time and store the text: the agents only ever need
        # text, and re-parsing a PDF on every run wastes time in a timed function.
        store.save_document(user["id"], name, text)
        saved.append(name)

    if not saved:
        raise HTTPException(400, "Only PDF, Markdown and text files are accepted")
    return {"saved": saved}


@app.delete("/api/documents/{filename}")
def delete_document(filename: str, user: dict = Depends(current_user)) -> dict:
    store.delete_document(user["id"], Path(filename).name)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #


class JobBody(BaseModel):
    external_id: str | None = None
    role: str | None = None
    limit: int = Field(default=25, ge=1, le=200)
    stage: str = "screen"


LABELS = {
    "profile": "Building your skill graph",
    "gaps": "Analysing your gaps",
    "discover": "Pulling live jobs",
    "score": "Scoring matches",
    "tailor": "Tailoring your resume",
    "outreach": "Drafting outreach",
    "prep": "Building interview prep",
    "intel": "Researching the company",
    "comp": "Estimating compensation",
}


@app.post("/api/jobs/{kind}")
def start_job(kind: str, body: JobBody, user: dict = Depends(current_user)) -> dict:
    if kind not in LABELS:
        raise HTTPException(404, f"unknown action: {kind}")
    if kind != "discover" and not store.get_user_key(user["id"]):
        raise HTTPException(400, "Add your API key in Settings first")

    payload: dict = {}
    if kind in handlers.SINGLE_JOB_KINDS:
        if not body.external_id:
            raise HTTPException(400, "external_id is required for this action")
        payload["external_id"] = body.external_id
        payload["stage"] = body.stage
    if kind == "gaps":
        if not (body.role or "").strip():
            raise HTTPException(400, "Describe the role you are targeting")
        payload["role"] = body.role.strip()
    if kind == "score":
        payload["limit"] = body.limit

    try:
        job = queue.enqueue(user["id"], kind, LABELS[kind], payload)
    except (RuntimeError, MissingKey) as e:
        raise HTTPException(400, str(e)) from e

    # Nudge the worker so short jobs finish within this request.
    if job["state"] == "queued":
        queue.work(budget_seconds=INLINE_WORKER_BUDGET, max_jobs=1)

    return {"job_id": job["id"]}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: int, user: dict = Depends(current_user)) -> dict:
    job = queue.get_job(user["id"], job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return {
        "id": job["id"], "kind": job["kind"], "label": job["label"], "state": job["state"],
        "log": job["log"], "result": job["result"], "error": job["error"],
        "units_done": job["units_done"], "units_total": job["units_total"],
        "progress": job["progress"],
    }


@app.post("/api/jobs/{job_id}/resume")
def resume_job(job_id: int, user: dict = Depends(current_user)) -> dict:
    """Let the browser push a long job along without waiting for the cron tick."""
    job = queue.get_job(user["id"], job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    if job["state"] in ("queued", "running"):
        queue.work(budget_seconds=INLINE_WORKER_BUDGET, max_jobs=1)
    return job_status(job_id, user)


# --------------------------------------------------------------------------- #
# matches, pipeline, artifacts
# --------------------------------------------------------------------------- #


@app.get("/api/matches")
def matches(limit: int = 40, min_score: int = 0, user: dict = Depends(current_user)) -> dict:
    rows = store.top_matches(user["id"], limit, min_score)
    return {
        "matches": [
            {"id": r["external_id"], "company": r["company"], "title": r["title"],
             "location": r["location"], "url": r["url"], "score": r["score"],
             "verdict": r["verdict"], "matched": r["matched_skills"] or [],
             "missing": r["missing_skills"] or [], "reasoning": r["reasoning"],
             "stage": r["stage"]}
            for r in rows
        ]
    }


class ShortlistBody(BaseModel):
    min_score: int = Field(default=70, ge=0, le=100)
    limit: int = Field(default=10, ge=1, le=50)


@app.post("/api/shortlist")
def shortlist(body: ShortlistBody, user: dict = Depends(current_user)) -> dict:
    rows = store.unpursued_strong(user["id"], body.min_score, body.limit)
    for r in rows:
        store.add_application(user["id"], r["external_id"])
        store.record_outcome(user["id"], "shortlisted", f"score {r['score']}", r["external_id"])
    return {"added": len(rows)}


@app.get("/api/pipeline")
def pipeline_view(user: dict = Depends(current_user)) -> dict:
    user_id = user["id"]
    apps = store.list_applications(user_id)
    counts = store.stage_counts(user_id)
    funnel = {stage: counts.get(stage, 0) for stage in pipeline.STAGES}

    followup, ghosts = [], []
    for a in apps:
        limit = pipeline.FOLLOWUP_AFTER.get(a["stage"])
        ghost = pipeline.GHOST_AFTER.get(a["stage"])
        days = a["days_in_stage"]
        if ghost is not None and days >= ghost:
            ghosts.append(a["external_id"])
        elif limit is not None and days >= limit:
            followup.append(a["external_id"])

    applied = sum(counts.get(s, 0) for s in
                  ("applied", "screen", "interview", "onsite", "offer", "rejected",
                   "ghosted", "accepted"))
    responded = sum(counts.get(s, 0) for s in
                    ("screen", "interview", "onsite", "offer", "accepted"))

    return {
        "stages": pipeline.STAGES,
        "funnel": funnel,
        "rates": {
            "applied_total": applied,
            # Below ten applications a percentage is noise dressed up as a metric.
            "callback_rate_pct": round(responded / applied * 100, 1) if applied >= 10 else None,
        },
        "applications": [
            {"id": a["external_id"], "company": a["company"], "title": a["title"],
             "stage": a["stage"], "score": a["score"], "days": a["days_in_stage"],
             "url": a["url"]}
            for a in apps
        ],
        "needs_followup": followup,
        "ghosts": ghosts,
    }


class StageBody(BaseModel):
    to: str
    note: str = ""


@app.post("/api/stage/{external_id:path}")
def set_stage(external_id: str, body: StageBody, user: dict = Depends(current_user)) -> dict:
    app_row = store.get_application(user["id"], external_id)
    if not app_row:
        raise HTTPException(404, "That job is not in your pipeline")
    if body.to not in pipeline.STAGES:
        raise HTTPException(400, f"unknown stage: {body.to}")
    if not pipeline.can_move(app_row["stage"], body.to):
        allowed = ", ".join(sorted(pipeline._FORWARD.get(app_row["stage"], set()))) or "nothing"
        raise HTTPException(400, f"cannot go {app_row['stage']} -> {body.to}. Allowed: {allowed}")

    store.set_stage(user["id"], external_id, body.to, body.note)
    store.record_outcome(user["id"], body.to, body.note, external_id)
    return {"id": external_id, "stage": body.to}


@app.get("/api/artifact/{kind}/{external_id:path}")
def artifact(kind: str, external_id: str, user: dict = Depends(current_user)) -> dict:
    payload = store.latest_artifact(user["id"], kind, external_id)
    if payload is None:
        raise HTTPException(404, f"no {kind} generated for this job yet")
    return {"kind": kind, "payload": payload}


@app.get("/api/next")
def next_actions(user: dict = Depends(current_user)) -> dict:
    """Agent 16's rule engine, reading the hosted store instead of SQLite."""
    user_id = user["id"]
    actions: list[dict] = []

    def add(label: str, why: str) -> None:
        actions.append({"label": label, "why": why})

    if not store.get_user_key(user_id):
        add("Add your API key", "Settings -> API key. Agents cannot run without it.")
        return {"actions": actions}
    if not store.list_documents(user_id):
        add("Upload your resume and brag document",
            "Everything downstream is built from these.")
        return {"actions": actions}
    if not store.load_profile(user_id):
        add("Build your skill graph", "Nothing else can run without it.")
        return {"actions": actions}
    if store.postings_count() == 0:
        add("Pull live jobs", "No postings in the database yet. This is free.")
    if not store.latest_gap_report(user_id):
        add("Run gap analysis", "Find out what is blocking you before spending applications.")
    if store.scored_count(user_id) == 0 and store.postings_count():
        add("Score your matches", "The free prefilter drops most postings before any cost.")

    strong = store.unpursued_strong_count(user_id)
    if strong:
        add(f"Shortlist {strong} strong match(es)", "Scored well but not yet in your pipeline.")

    for a in store.list_applications(user_id, "shortlisted"):
        add(f"Tailor resume: {a['company']} — {a['title'][:40]}",
            "Shortlisted but no tailored resume yet.")
    for a in store.list_applications(user_id, "offer"):
        add(f"Negotiate the offer from {a['company']}",
            "The highest-leverage moment in the whole process.")

    return {"actions": actions[:8]}


@app.get("/api/spend")
def spend(user: dict = Depends(current_user)) -> dict:
    return {
        "total": round(store.total_spend(user["id"]), 4),
        "limit": float(user["spend_limit_usd"]),
        "by_agent": store.spend_by_agent(user["id"]),
    }


# --------------------------------------------------------------------------- #
# worker (cron)
# --------------------------------------------------------------------------- #


@app.post("/api/worker")
@app.get("/api/worker")
def worker(request: Request) -> dict:
    """Drains the queue. Invoked by the platform scheduler, not by users.

    Guarded by CRON_SECRET so a public URL cannot be used to burn function time.
    """
    expected = os.getenv("CRON_SECRET", "").strip()
    if expected:
        supplied = request.headers.get("authorization", "")
        if supplied != f"Bearer {expected}":
            raise HTTPException(401, "unauthorised")

    store.purge_expired_sessions()
    budget = float(os.getenv("WORKER_BUDGET", "45"))
    return queue.work(budget_seconds=budget, max_jobs=10)


# --------------------------------------------------------------------------- #
# per-user target companies
# --------------------------------------------------------------------------- #


class BoardsBody(BaseModel):
    companies: dict


@app.get("/api/settings/companies")
def get_companies(user: dict = Depends(current_user)) -> dict:
    """The user's own board list, or the shipped default if they have not set one."""
    from codeskate.agents import discovery

    own = store.load_boards(user["id"])
    return {
        "companies": own or discovery.load_company_config(),
        "is_custom": own is not None,
        "sources": sorted(discovery.ALL_SOURCES),
    }


@app.post("/api/settings/companies")
def set_companies(body: BoardsBody, user: dict = Depends(current_user)) -> dict:
    from codeskate.agents import discovery

    unknown = set(body.companies) - discovery.ALL_SOURCES
    if unknown:
        raise HTTPException(400, f"unsupported source(s): {', '.join(sorted(unknown))}")
    total = sum(len(v) for v in body.companies.values())
    if total > 300:
        raise HTTPException(400, "Keep it under 300 boards — discovery would take too long")

    store.save_boards(user["id"], body.companies)
    return {"boards": total}


@app.delete("/api/settings/companies")
def reset_companies(user: dict = Depends(current_user)) -> dict:
    config = store.load_targets(user["id"]) or {}
    config.pop("companies", None)
    store.save_targets(user["id"], config)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# admin
# --------------------------------------------------------------------------- #


def current_admin(user: dict = Depends(current_user)) -> dict:
    if not admin.is_admin(user["email"]):
        # 404 rather than 403: a non-admin has no business learning that this
        # surface exists at all.
        raise HTTPException(404, "Not found")
    return user


@app.get("/api/admin/overview")
def admin_overview(user: dict = Depends(current_admin)) -> dict:
    return admin.overview()


@app.get("/api/admin/users")
def admin_users(limit: int = 100, offset: int = 0,
                user: dict = Depends(current_admin)) -> dict:
    return admin.users(min(limit, 500), max(offset, 0))


class ActiveBody(BaseModel):
    is_active: bool


@app.post("/api/admin/users/{user_id}/active")
def admin_set_active(user_id: int, body: ActiveBody,
                     user: dict = Depends(current_admin)) -> dict:
    if user_id == user["id"] and not body.is_active:
        raise HTTPException(400, "You cannot disable your own account")
    admin.set_active(user_id, body.is_active)
    return {"id": user_id, "is_active": body.is_active}


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


# --------------------------------------------------------------------------- #
# legal
# --------------------------------------------------------------------------- #


@app.get("/privacy")
def privacy() -> FileResponse:
    return FileResponse(STATIC / "privacy.html")


@app.get("/terms")
def terms() -> FileResponse:
    return FileResponse(STATIC / "terms.html")


@app.get("/reset")
def reset_page() -> FileResponse:
    """The password-reset link lands here; the SPA reads the token from the query."""
    return FileResponse(STATIC / "index.html")


# --------------------------------------------------------------------------- #
# static
# --------------------------------------------------------------------------- #


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
