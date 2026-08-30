"""Multi-tenant hosted version of CodeSkate.

Layering, and what changed from the single-user tool:

    schema.py    SQLAlchemy tables. Shared postings, per-user everything else.
    engine.py    Engine + dialect-neutral upsert. Postgres in production.
    store.py     Data access. Per-user functions take user_id first, always.
    auth.py      scrypt password hashing, hashed session tokens.
    crypto.py    Fernet encryption for bring-your-own-key API keys.
    runtime.py   Builds an LLM client bound to one user's key and spend ceiling.
    queue.py     Postgres job queue, work split into timeout-sized units.
    handlers.py  One unit of agent work per job kind.
    app.py       FastAPI surface.

The eighteen agents in `codeskate.agents` are untouched. Only persistence, the
auth boundary and the execution model differ — which is the whole reason the
agents were written as plain functions over explicit arguments.
"""
