"""Database engine and a dialect-neutral upsert.

DATABASE_URL selects the backend. Postgres in production; SQLite is supported so
the suite can run without a server, and so local runs work offline.

Works with any managed Postgres — Neon, Supabase, RDS. The code is plain
SQLAlchemy Core and has no opinion about the host.

Two things matter and both are easy to get wrong:

* **Use the pooled endpoint.** Serverless functions are created and discarded
  constantly, and each one opening a direct connection will exhaust a small
  instance's connection limit. Neon and Supabase both publish a pooled URL.

* **Turn off server-side prepared statements.** Transaction-mode poolers
  (Supabase's Supavisor on port 6543, PgBouncer in transaction mode) do not
  support them, and psycopg3 enables them automatically after a few executions.
  See the connect_args below.
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
            connect_args={
                # Server-side prepared statements must be off behind a
                # transaction-mode pooler. Supabase's Supavisor on port 6543 and
                # PgBouncer in transaction mode both reject them, while psycopg3
                # starts using them automatically after the fifth execution of a
                # query. Since this app runs the same session lookup on every
                # request, that threshold is crossed within seconds and the
                # errors appear only under load — the worst way to find out.
                #
                # Set unconditionally rather than by sniffing the port: a direct
                # connection loses a small optimisation, a pooled one breaks.
                "prepare_threshold": None,
            },
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
