"""Database engine and a dialect-neutral upsert.

DATABASE_URL selects the backend. Postgres in production; SQLite is supported so
the suite can run without a server, and so `vercel dev` works offline.

Connection pooling matters more than usual here. Serverless functions come and go,
and a pool that holds connections open across cold starts will exhaust a small
Postgres instance. Neon and Supabase both offer a pooled endpoint (pgbouncer);
use it, and keep the local pool tiny.
"""

from __future__ import annotations

import os
from typing import Any

from sqlalchemy import Table, create_engine, insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from .schema import metadata

_engine: Engine | None = None


def database_url() -> str:
    url = os.getenv("DATABASE_URL", "").strip()
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Use a Postgres URL in production — SQLite "
            "cannot work on Vercel, where storage is ephemeral and unshared."
        )
    # Neon and Heroku hand out postgres:// ; SQLAlchemy 2 wants postgresql://
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


def get_engine() -> Engine:
    global _engine
    if _engine is not None:
        return _engine

    url = database_url()
    if url.startswith("sqlite"):
        _engine = create_engine(url, future=True)
    else:
        _engine = create_engine(
            url,
            future=True,
            pool_size=1,
            max_overflow=2,
            pool_pre_ping=True,   # a pooled connection may have been closed under us
            pool_recycle=280,
        )
    return _engine


def reset_engine() -> None:
    """Drop the cached engine. Used by tests that switch databases."""
    global _engine
    if _engine is not None:
        _engine.dispose()
    _engine = None


def create_all() -> None:
    metadata.create_all(get_engine())


def upsert(table: Table, values: dict[str, Any] | list[dict[str, Any]], index_elements: list[str],
           update_cols: list[str]):
    """Build an INSERT .. ON CONFLICT DO UPDATE for whichever dialect is active."""
    dialect = get_engine().dialect.name
    if dialect == "postgresql":
        stmt = pg_insert(table).values(values)
    elif dialect == "sqlite":
        stmt = sqlite_insert(table).values(values)
    else:  # pragma: no cover - no other backend is supported
        return insert(table).values(values)

    return stmt.on_conflict_do_update(
        index_elements=index_elements,
        set_={c: getattr(stmt.excluded, c) for c in update_cols},
    )


def upsert_nothing(table: Table, values: dict[str, Any], index_elements: list[str]):
    dialect = get_engine().dialect.name
    stmt = pg_insert(table) if dialect == "postgresql" else sqlite_insert(table)
    return stmt.values(values).on_conflict_do_nothing(index_elements=index_elements)
