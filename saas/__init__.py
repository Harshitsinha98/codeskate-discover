"""Multi-tenant hosted version of CodeSkate.

Layering, and what changed from the single-user tool:

    schema.py    SQLAlchemy tables. Shared postings, per-user everything else.
    engine.py    Engine + dialect-neutral upsert. Postgres in production.
    store.py     Data access. Per-user functions take user_id first, always.
    auth.py      Session tokens (hashed at rest). No passwords — Google only.
    google_auth.py  OAuth authorization code flow.
    plans.py     Free and Pro definitions.
    quota.py     Run accounting and enforcement, plus a global daily circuit breaker.
    billing.py   Razorpay orders, signature and webhook verification.
    runtime.py   Builds an LLM client on the platform key, recording usage per user.
    admin.py     Owner metrics. Never exposes user content.
    queue.py     Postgres job queue, work split into timeout-sized units.
    handlers.py  One unit of agent work per job kind.
    app.py       FastAPI surface.

The eighteen agents in `codeskate.agents` are untouched. Only persistence, the
auth boundary and the execution model differ — which is the whole reason the
agents were written as plain functions over explicit arguments.
"""
