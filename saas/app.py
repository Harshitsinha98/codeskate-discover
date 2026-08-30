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
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import Cookie, Depends, FastAPI, File, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from codeskate.agents import career_manager, pipeline
from codeskate.models import SkillGraph

from . import (  # handlers registers kinds
    admin,
    auth,
    billing,
    google_auth,
    handlers,
    plans,
    presets,
    queue,
    quota,
    store,
)
from .engine import create_all
from . import runtime
from .runtime import PlatformKeyMissing

STATIC = Path(__file__).parent / "static"
SECURE_COOKIES = os.getenv("SECURE_COOKIES", "1") != "0"
INLINE_WORKER_BUDGET = float(os.getenv("INLINE_WORKER_BUDGET", "12"))

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start an in-process queue worker when running on a real server.

    On a host that keeps a process alive (Railway, Render, Fly, a VM) this removes
    the need for an external scheduler entirely: a background thread drains the
    queue continuously, so a long job finishes whether or not the user's tab is
    open. Serverless deployments leave WORKER_IN_PROCESS unset and rely on cron
    plus the browser nudging /api/jobs/{id}/resume.

    This is why the platform choice matters more than it looks. Vercel's Hobby
    plan caps cron at once per day, which would leave an abandoned job queued for
    up to 24 hours.
    """
    stop = threading.Event()
    thread: threading.Thread | None = None

    if os.getenv("WORKER_IN_PROCESS", "0") == "1":
        create_all()
        global _schema_ready
        _schema_ready = True

        def loop() -> None:
            while not stop.wait(float(os.getenv("WORKER_POLL_SECONDS", "3"))):
                try:
                    queue.work(budget_seconds=20.0, max_jobs=3)
                except Exception as e:  # noqa: BLE001 - a bad job must not kill the worker
                    print(f"worker loop error: {type(e).__name__}: {e}", flush=True)

        thread = threading.Thread(target=loop, daemon=True, name="codeskate-worker")
        thread.start()
        print("in-process queue worker started", flush=True)

    try:
        yield
    finally:
        stop.set()
        if thread:
            thread.join(timeout=5)


app = FastAPI(title="CodeSkate", docs_url=None, redoc_url=None, lifespan=lifespan)

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


# --------------------------------------------------------------------------- #
# Google sign-in
# --------------------------------------------------------------------------- #


@app.get("/api/auth/google/start")
def google_start(response: Response) -> dict:
    """Hand the browser a Google URL plus a state cookie to be echoed back."""
    if not google_auth.configured():
        raise HTTPException(
            503,
            "Google sign-in is not configured on this deployment. The operator must "
            "set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        )
    state = google_auth.new_state()
    response.set_cookie(value=state, **auth.state_cookie_kwargs(SECURE_COOKIES))
    return {"url": google_auth.authorize_url(state)}


@app.get("/api/auth/google/callback")
def google_callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    cs_oauth_state: str | None = Cookie(default=None),
) -> RedirectResponse:
    """Where Google returns the user. Always ends in a redirect, never JSON."""
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/") or str(request.base_url).rstrip("/")

    def fail(reason: str) -> RedirectResponse:
        # Failures land on the sign-in page, not the marketing page: someone who
        # just tried to sign in wants to try again, not read the pitch.
        return RedirectResponse(f"{base}/signin?auth_error={quote(reason)}", status_code=303)

    if error:
        return fail("Sign-in was cancelled")
    if not code:
        return fail("Google did not return an authorization code")
    # The state cookie is the CSRF defence: without it, an attacker could feed a
    # victim's browser a code of their choosing.
    if not state or not cs_oauth_state or not secrets.compare_digest(state, cs_oauth_state):
        return fail("Sign-in session expired — please try again")

    try:
        identity = google_auth.exchange_code(code)
    except google_auth.GoogleAuthError as e:
        return fail(str(e))

    user = store.upsert_google_user(
        identity["sub"], identity["email"], identity["name"], identity.get("picture")
    )
    if not user["is_active"]:
        return fail("This account has been disabled")

    session = auth.new_session()
    store.create_session(session.token_hash, user["id"])

    redirect = RedirectResponse(f"{base}/app", status_code=303)
    redirect.set_cookie(value=session.token, **auth.cookie_kwargs(SECURE_COOKIES))
    redirect.delete_cookie(auth.STATE_COOKIE, path="/")
    return redirect


@app.post("/api/auth/logout")
def logout(response: Response, cs_session: str | None = Cookie(default=None)) -> dict:
    if cs_session:
        store.delete_session(auth.hash_token(cs_session))
    response.delete_cookie(auth.COOKIE_NAME, path="/")
    return {"ok": True}


@app.get("/api/config")
def public_config() -> dict:
    """What the marketing, pricing and sign-in pages need before authentication."""
    return {
        "google_configured": google_auth.configured(),
        "billing_configured": billing.configured(),
        "plans": plans.catalogue(),
        "presets": presets.catalogue(),
        "cities": presets.city_catalogue(),
    }


# --------------------------------------------------------------------------- #
# billing
# --------------------------------------------------------------------------- #


class CheckoutBody(BaseModel):
    plan: str = Field(default="pro", pattern="^(pro)$")
    months: int = Field(default=1, ge=1, le=12)


@app.post("/api/billing/checkout")
def billing_checkout(body: CheckoutBody, user: dict = Depends(current_user)) -> dict:
    if not billing.configured():
        raise HTTPException(503, "Payments are not configured on this deployment yet.")
    try:
        return billing.create_order(user["id"], body.plan, body.months)
    except billing.BillingError as e:
        raise HTTPException(400, str(e)) from e


class ConfirmBody(BaseModel):
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str
    plan: str = "pro"
    months: int = Field(default=1, ge=1, le=12)


@app.post("/api/billing/confirm")
def billing_confirm(body: ConfirmBody, user: dict = Depends(current_user)) -> dict:
    """Called by the browser after Checkout succeeds.

    A valid signature proves the values came from Razorpay; fetching the payment
    proves it was actually captured and for how much. Both are required — the
    browser is not a trustworthy narrator of its own purchase.
    """
    if not billing.verify_signature(
        body.razorpay_order_id, body.razorpay_payment_id, body.razorpay_signature
    ):
        raise HTTPException(400, "Payment signature did not verify")

    try:
        payment = billing.fetch_payment(body.razorpay_payment_id)
    except billing.BillingError as e:
        raise HTTPException(502, str(e)) from e

    if payment.get("status") not in ("captured", "authorized"):
        raise HTTPException(400, f"Payment is not complete (status: {payment.get('status')})")

    expected = plans.plan_for(body.plan).price_inr * 100 * body.months
    if int(payment.get("amount") or 0) < expected:
        raise HTTPException(400, "Paid amount does not match the selected plan")

    fresh = store.record_payment(
        user["id"], body.razorpay_payment_id, body.razorpay_order_id,
        int(payment["amount"]), payment["status"], body.plan, body.months, payment,
    )
    if fresh:
        expiry = store.extend_plan(user["id"], body.plan, body.months)
    else:
        # Already applied, most likely by the webhook arriving first.
        expiry = store.user_by_id(user["id"])["plan_expires_at"]

    return {"plan": body.plan, "expires_at": str(expiry), "already_applied": not fresh}


@app.post("/api/billing/webhook")
async def billing_webhook(request: Request) -> dict:
    """Server-to-server confirmation, so a closed tab cannot lose a paid month."""
    body = await request.body()
    signature = request.headers.get("x-razorpay-signature", "")
    if not billing.verify_webhook(body, signature):
        raise HTTPException(400, "invalid webhook signature")

    parsed = billing.parse_webhook_payment(body)
    if not parsed or not parsed["user_id"] or not parsed["plan"]:
        return {"ignored": True}

    fresh = store.record_payment(
        parsed["user_id"], parsed["payment_id"], parsed["order_id"],
        parsed["amount_paise"], parsed["status"], parsed["plan"], parsed["months"],
        parsed["raw"],
    )
    if fresh:
        store.extend_plan(parsed["user_id"], parsed["plan"], parsed["months"])
    return {"applied": fresh}


@app.get("/api/billing/history")
def billing_history(user: dict = Depends(current_user)) -> dict:
    return {"payments": store.list_payments(user["id"])}


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #


class TargetsBody(BaseModel):
    config: dict


@app.post("/api/settings/targets")
def save_targets(body: TargetsBody, user: dict = Depends(current_user)) -> dict:
    store.save_targets(user["id"], body.config)
    return {"ok": True}


# --------------------------------------------------------------------------- #
# what you are looking for
# --------------------------------------------------------------------------- #


class SetupBody(BaseModel):
    roles: list[str] = Field(min_length=1, max_length=4)
    cities: list[str] = Field(default_factory=list, max_length=20)
    years: float = Field(default=0, ge=0, le=50)
    min_salary_lpa: float = Field(default=0, ge=0, le=500)
    current_role: str = Field(default="", max_length=200)
    remote_ok: bool = True
    avoid: list[str] = Field(default_factory=list, max_length=40)


@app.get("/api/setup")
def get_setup(user: dict = Depends(current_user)) -> dict:
    """What the user is looking for, plus a suggestion if they have not said yet.

    The suggestion is derived from their own skill graph by keyword counting — free
    and instant — so a new account is never shown an empty form it does not know
    how to fill in.
    """
    saved = store.load_targets(user["id"]) or {}
    graph = store.load_profile(user["id"])
    constraints = saved.get("constraints") or {}

    return {
        "configured": bool(saved.get("preset_keys")),
        "roles": saved.get("preset_keys") or [],
        "cities": saved.get("cities") or list(presets.DEFAULT_CITIES),
        "years": constraints.get("years_of_experience")
        or (graph or {}).get("total_years_experience")
        or 0,
        "min_salary_lpa": constraints.get("minimum_salary_inr_lpa") or 0,
        "current_role": constraints.get("current_role") or (graph or {}).get("headline") or "",
        "remote_ok": saved.get("remote_ok", True),
        "avoid": [t.strip() for t in saved.get("avoid") or []],
        "suggested_roles": presets.suggest(graph),
        "catalogue": presets.catalogue(),
        "all_cities": presets.city_catalogue(),
        # The raw lists, for anyone who wants to see or tune what the choice produced.
        "title_include": saved.get("title_include") or [],
        "title_exclude": saved.get("title_exclude") or [],
    }


@app.post("/api/setup")
def post_setup(body: SetupBody, user: dict = Depends(current_user)) -> dict:
    """Turn the four questions on the setup screen into a full filter config."""
    existing = store.load_targets(user["id"]) or {}
    try:
        config = presets.targets_for(
            body.roles,
            years=body.years,
            cities=body.cities or list(presets.DEFAULT_CITIES),
            remote_ok=body.remote_ok,
            min_salary_lpa=body.min_salary_lpa,
            current_role=body.current_role,
            avoid=body.avoid,
            keep_constraints=existing.get("constraints") or {},
        )
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    config["avoid"] = body.avoid
    store.save_targets(user["id"], config)
    return {
        "roles": config["preset_keys"],
        "cities": config["cities"],
        "title_include": config["title_include"],
        "title_exclude": config["title_exclude"],
    }


class RulesBody(BaseModel):
    title_include: list[str] = Field(default_factory=list, max_length=200)
    title_exclude: list[str] = Field(default_factory=list, max_length=200)


@app.post("/api/settings/rules")
def save_rules(body: RulesBody, user: dict = Depends(current_user)) -> dict:
    """Overwrite only the two title lists, keeping everything else intact.

    The advanced editor previously posted a whole config, which meant hand-editing
    the title lists silently discarded the locations and constraints alongside them.
    Merging is the only safe shape for a partial editor.
    """
    config = store.load_targets(user["id"]) or {}
    if not config:
        raise HTTPException(400, "Choose what you are looking for first")

    include = [t.strip().lower() for t in body.title_include if t.strip()]
    exclude = [t.strip().lower() for t in body.title_exclude if t.strip()]
    if not include:
        raise HTTPException(400, "Leave at least one entry in the 'must contain' list")

    config["title_include"] = include
    # Re-padded so a hand-typed "sales" behaves as a whole word rather than also
    # matching Salesforce.
    config["title_exclude"] = presets.pad_exclusions(exclude)
    store.save_targets(user["id"], config)
    return {"title_include": config["title_include"], "title_exclude": config["title_exclude"]}


@app.get("/api/diagnostics")
def diagnostics(user: dict = Depends(current_user)) -> dict:
    """Why the match list looks the way it does. Free, no credits spent.

    Added because a user watched the free filter report `1368 unscored -> 0
    plausible` and reasonably concluded the product was broken. Nothing was broken;
    the system just never said which rule emptied the list.
    """
    return handlers.match_diagnostics(user["id"])


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #


@app.get("/api/me")
def me(user: dict = Depends(current_user)) -> dict:
    user_id = user["id"]
    raw = store.load_profile(user_id)
    graph = SkillGraph.model_validate(raw) if raw else None
    counts = store.stage_counts(user_id)
    targets = store.load_targets(user_id) or {}

    return {
        "email": user["email"],
        # Drives which screen the app opens on. Three facts, in the order they
        # have to be true: resume, what you want, results.
        "setup": {
            "has_documents": bool(store.list_documents(user_id)),
            "has_profile": graph is not None,
            "has_targets": bool(targets.get("preset_keys")),
            "roles": targets.get("preset_keys") or [],
            "cities": targets.get("cities") or [],
        },
        "is_admin": admin.is_admin(user["email"]),
        "display_name": user.get("display_name"),
        "avatar_url": user.get("avatar_url"),
        # Runs, not dollars. What a call costs is the operator's problem.
        "quota": quota.status(user),
        "billing_configured": billing.configured(),
        "platform_ready": runtime.configured(),
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


# Shown verbatim in the progress toast, so these are written for the person
# waiting rather than for whoever wrote the handler.
LABELS = {
    "profile": "Reading your resume",
    "gaps": "Checking how ready you are",
    "discover": "Searching company career pages",
    "score": "Finding your matches",
    "tailor": "Rewriting your resume for this job",
    "outreach": "Writing your messages",
    "prep": "Preparing you for the interview",
    "intel": "Researching the company",
    "comp": "Working out the salary band",
}


@app.post("/api/jobs/{kind}")
def start_job(kind: str, body: JobBody, user: dict = Depends(current_user)) -> dict:
    if kind not in LABELS:
        raise HTTPException(404, f"unknown action: {kind}")
    # Discovery is free and uses no model, so it is not metered.
    if kind != "discover":
        try:
            quota.check(user, kind)
        except quota.QuotaExceeded as e:
            raise HTTPException(402, str(e)) from e
        if not runtime.configured():
            raise HTTPException(
                503, "The service is not fully configured yet. Please try again later."
            )

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
        # Clamp to what the plan and the remaining allowance permit, rather than
        # queueing work that will die part-way through.
        allowed = quota.batch_limit(user, body.limit)
        if allowed == 0:
            raise HTTPException(402, quota.status(user)["plan"] == "free"
                                and "You have used your free agent runs for this month. "
                                    "Upgrade to Pro to keep scoring."
                                or "You have used this month's agent runs.")
        payload["limit"] = allowed

    try:
        job = queue.enqueue(user["id"], kind, LABELS[kind], payload)
    except (RuntimeError, PlatformKeyMissing) as e:
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


@app.post("/api/save/{external_id:path}")
def save_job(external_id: str, user: dict = Depends(current_user)) -> dict:
    """Put one job on the user's board.

    Previously the only way in was the bulk "shortlist everything above 70"
    action, so a job the user personally liked at 62 could not be saved at all
    without paying to generate a resume for it first.
    """
    if not store.posting(external_id):
        raise HTTPException(404, "That job posting is no longer available")

    added = store.add_application(user["id"], external_id)
    if added:
        store.record_outcome(user["id"], "shortlisted", "saved from matches", external_id)
    return {"saved": True, "already_saved": not added}


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
    """The one thing worth doing next, in the user's words rather than the system's.

    Each entry carries an `action` the interface can wire to a button, so this is a
    to-do list you can act on rather than a description of one.
    """
    user_id = user["id"]
    actions: list[dict] = []

    def add(label: str, why: str, action: str = "") -> None:
        actions.append({"label": label, "why": why, "action": action})

    if not store.list_documents(user_id):
        add("Add your resume",
            "Everything here is built from it. A PDF is fine.", "upload")
        return {"actions": actions}
    if not store.load_profile(user_id):
        add("Let us read your resume",
            "One pass to pull out your skills and what you can prove.", "profile")
        return {"actions": actions}
    if not (store.load_targets(user_id) or {}).get("preset_keys"):
        add("Tell us what you are looking for",
            "Pick the kind of role and the cities. This is what decides your matches.",
            "setup")
        return {"actions": actions}
    if store.postings_count() == 0:
        add("Search for openings",
            "We read company career pages directly. Free, takes about a minute.",
            "discover")
        return {"actions": actions}
    if store.scored_count(user_id) == 0:
        add("See your matches",
            "Each job gets a score out of 100 and a written reason.", "score")
        return {"actions": actions}

    strong = store.unpursued_strong_count(user_id)
    if strong:
        add(f"Save {strong} strong match{'es' if strong > 1 else ''}",
            "These scored well and you have not started on them yet.", "shortlist")

    for a in store.list_applications(user_id, "shortlisted")[:3]:
        add(f"Rewrite your resume for {a['company']}",
            f"{a['title'][:60]} — saved, but nothing prepared yet.", "tailor")
    for a in store.list_applications(user_id, "offer"):
        add(f"Negotiate the offer from {a['company']}",
            "The single highest-paid hour of the whole search.", "negotiate")

    if not store.latest_gap_report(user_id):
        add("Find out what is holding you back",
            "An honest read on what to learn next, and the fastest way to prove it.",
            "gaps")

    if not actions:
        add("Keep your search moving",
            "Nothing is waiting on you. Search again in a few days for new openings.",
            "discover")

    return {"actions": actions[:6]}


@app.get("/api/usage")
def usage(user: dict = Depends(current_user)) -> dict:
    """Usage in runs. Deliberately no dollar figures: the model bill is the
    operator's concern, and showing it would invite users to optimise the wrong
    thing."""
    rows = store.spend_by_agent(user["id"])
    return {
        "quota": quota.status(user),
        "by_agent": [
            {"agent": r["agent"], "runs": r["calls"],
             "input_tokens": r["tin"], "output_tokens": r["tout"]}
            for r in rows
        ],
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


@app.get("/refunds")
def refunds() -> FileResponse:
    """Razorpay's KYC review asks for a reachable refund policy, and a paid product
    with no stated policy is a fair thing for them to reject."""
    return FileResponse(STATIC / "refunds.html")


@app.get("/contact")
def contact() -> FileResponse:
    return FileResponse(STATIC / "contact.html")


# --------------------------------------------------------------------------- #
# pages
# --------------------------------------------------------------------------- #
#
# `/` is the marketing page and `/app` is the product. They were the same file
# before, which meant the first thing a visitor saw was a sign-in button with no
# explanation of what they would be signing in to.


@app.get("/")
def home() -> FileResponse:
    return FileResponse(STATIC / "home.html")


@app.get("/pricing")
def pricing() -> FileResponse:
    return FileResponse(STATIC / "pricing.html")


@app.get("/signin")
def signin() -> FileResponse:
    return FileResponse(STATIC / "signin.html")


@app.get("/app")
def app_page() -> FileResponse:
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
