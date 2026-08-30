"""Multi-tenant schema, SQLAlchemy Core.

Written dialect-neutral so it runs on Postgres in production and SQLite in tests.
Postgres is the production target because Vercel's filesystem is read-only apart
from an ephemeral /tmp, and concurrent function instances do not share storage —
SQLite there would silently lose every user's data.

Two classes of table, and the split matters commercially:

  SHARED    job_postings, company_intel
            A posting is identical for every user, so it is fetched once and read
            by everyone. Discovery cost and rate-limit pressure stay flat as users
            grow instead of multiplying by user count.

  PER-USER  everything else, keyed by user_id
            Scores, pipeline, artifacts and spend are personal. Every read is
            scoped by user_id in store.py — there is no unscoped accessor.
"""

from __future__ import annotations

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    func,
)

metadata = MetaData()

# --------------------------------------------------------------------------- #
# accounts
# --------------------------------------------------------------------------- #

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("email", String(320), nullable=False, unique=True),
    Column("password_hash", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("last_login_at", DateTime(timezone=True)),
    # Per-user ceiling on their own key. Defence against a runaway loop.
    Column("spend_limit_usd", Float, nullable=False, default=5.0),
    Column("is_active", Boolean, nullable=False, default=True),
)

# Bring-your-own-key, encrypted at rest. Stored separately from `users` so a
# query that only needs account fields cannot accidentally select ciphertext.
user_keys = Table(
    "user_keys",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("provider", String(16), nullable=False),
    Column("key_ciphertext", Text, nullable=False),
    Column("key_hint", String(16), nullable=False),  # last 4 chars, for the UI
    Column("model_cheap", String(64)),
    Column("model_smart", String(64)),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

sessions = Table(
    "sessions",
    metadata,
    Column("token_hash", String(64), primary_key=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Index("ix_sessions_user", "user_id"),
)

# --------------------------------------------------------------------------- #
# shared across all tenants
# --------------------------------------------------------------------------- #

job_postings = Table(
    "job_postings",
    metadata,
    Column("external_id", String(160), primary_key=True),
    Column("source", String(24), nullable=False),
    Column("company", String(120), nullable=False),
    Column("title", Text, nullable=False),
    Column("location", Text),
    Column("url", Text, nullable=False),
    Column("description", Text),
    Column("fetched_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_postings_company", "company"),
)

# Cached per company, not per user: the briefing does not differ between users,
# so one paid call serves everyone who applies there.
company_intel = Table(
    "company_intel",
    metadata,
    Column("company", String(120), primary_key=True),
    Column("intel", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
)

# --------------------------------------------------------------------------- #
# per-user
# --------------------------------------------------------------------------- #

profiles = Table(
    "profiles",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("graph", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

# Raw uploaded text, so the profile can be rebuilt after a prompt change without
# asking the user to upload again.
documents = Table(
    "documents",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("filename", String(255), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "filename", name="uq_documents_user_filename"),
)

targets = Table(
    "targets",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    Column("config", JSON, nullable=False),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
)

fit_scores = Table(
    "fit_scores",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160), ForeignKey("job_postings.external_id"), nullable=False),
    Column("score", Integer, nullable=False),
    Column("verdict", String(12), nullable=False),
    Column("matched_skills", JSON),
    Column("missing_skills", JSON),
    Column("reasoning", Text),
    Column("scored_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "external_id", name="uq_fit_user_job"),
    Index("ix_fit_user_score", "user_id", "score"),
)

applications = Table(
    "applications",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160), ForeignKey("job_postings.external_id"), nullable=False),
    Column("stage", String(20), nullable=False, default="shortlisted"),
    Column("stage_entered_at", DateTime(timezone=True), server_default=func.now()),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("notes", Text, default=""),
    UniqueConstraint("user_id", "external_id", name="uq_app_user_job"),
    Index("ix_app_user_stage", "user_id", "stage"),
)

artifacts = Table(
    "artifacts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160)),
    Column("kind", String(32), nullable=False),
    Column("payload", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_artifacts_lookup", "user_id", "kind", "external_id"),
)

gap_reports = Table(
    "gap_reports",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("target_role", Text, nullable=False),
    Column("report", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_gaps_user", "user_id"),
)

comp_estimates = Table(
    "comp_estimates",
    metadata,
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160), nullable=False),
    Column("estimate", JSON, nullable=False),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    UniqueConstraint("user_id", "external_id", name="uq_comp_user_job"),
)

mock_sessions = Table(
    "mock_sessions",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160)),
    Column("kind", String(24), nullable=False),
    Column("transcript", JSON, nullable=False),
    Column("avg_score", Float),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_mocks_user", "user_id"),
)

outcomes = Table(
    "outcomes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("external_id", String(160)),
    Column("event", String(32), nullable=False),
    Column("detail", Text, default=""),
    Column("occurred_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_outcomes_user", "user_id"),
)

llm_calls = Table(
    "llm_calls",
    metadata,
    # BIGINT on Postgres because this table grows fastest, but SQLite only
    # autoincrements a column declared INTEGER PRIMARY KEY — a BIGINT one raises a
    # NOT NULL violation on insert. with_variant gives each dialect what it needs.
    Column(
        "id",
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    ),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("agent", String(32), nullable=False),
    Column("model", String(64), nullable=False),
    Column("input_tokens", Integer, default=0),
    Column("output_tokens", Integer, default=0),
    Column("cache_write", Integer, default=0),
    Column("cache_read", Integer, default=0),
    Column("cost_usd", Float, default=0),
    Column("called_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_calls_user", "user_id"),
)

# --------------------------------------------------------------------------- #
# job queue
# --------------------------------------------------------------------------- #
#
# Replaces the in-process task registry the single-user app used. On Vercel that
# registry was unusable: functions are stateless, so the request that polls for
# progress may land on a different instance than the one doing the work.
#
# Long work is also split into units small enough to finish inside a function
# timeout — discovery becomes one unit per company board rather than one unit for
# all of them.

queue_jobs = Table(
    "queue_jobs",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("kind", String(32), nullable=False),
    Column("label", Text, nullable=False),
    Column("state", String(16), nullable=False, default="queued"),
    Column("payload", JSON, nullable=False, default=dict),
    Column("log", JSON, nullable=False, default=list),
    Column("result", JSON),
    Column("error", Text),
    # Progress for a chunked job: units done out of total.
    Column("units_done", Integer, nullable=False, default=0),
    Column("units_total", Integer, nullable=False, default=1),
    Column("attempts", Integer, nullable=False, default=0),
    Column("locked_at", DateTime(timezone=True)),
    Column("created_at", DateTime(timezone=True), server_default=func.now()),
    Column("updated_at", DateTime(timezone=True), server_default=func.now()),
    Index("ix_queue_state", "state"),
    Index("ix_queue_user", "user_id"),
)
