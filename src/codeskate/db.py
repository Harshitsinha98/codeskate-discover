"""SQLite persistence. No Postgres needed until you have real users."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from .settings import DATA_DIR, DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    external_id TEXT PRIMARY KEY,
    source      TEXT NOT NULL,
    company     TEXT NOT NULL,
    title       TEXT NOT NULL,
    location    TEXT,
    url         TEXT NOT NULL,
    description TEXT,
    fetched_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS profile (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    graph_json TEXT NOT NULL,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS fit_scores (
    external_id     TEXT PRIMARY KEY REFERENCES jobs(external_id),
    score           INTEGER NOT NULL,
    verdict         TEXT NOT NULL,
    matched_skills  TEXT,
    missing_skills  TEXT,
    reasoning       TEXT,
    scored_at       TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Every API call is logged. This is how the spend guard works, and how you
-- learn your real cost per user before you ever charge anyone.
CREATE TABLE IF NOT EXISTS llm_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent           TEXT NOT NULL,
    model           TEXT NOT NULL,
    input_tokens    INTEGER DEFAULT 0,
    output_tokens   INTEGER DEFAULT 0,
    cache_write     INTEGER DEFAULT 0,
    cache_read      INTEGER DEFAULT 0,
    cost_usd        REAL DEFAULT 0,
    called_at       TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def total_spend(conn: sqlite3.Connection) -> float:
    row = conn.execute("SELECT COALESCE(SUM(cost_usd), 0) AS t FROM llm_calls").fetchone()
    return float(row["t"])


def log_call(conn: sqlite3.Connection, **kw: Any) -> None:
    conn.execute(
        """INSERT INTO llm_calls
           (agent, model, input_tokens, output_tokens, cache_write, cache_read, cost_usd)
           VALUES (:agent, :model, :input_tokens, :output_tokens,
                   :cache_write, :cache_read, :cost_usd)""",
        kw,
    )
    conn.commit()


def save_profile(conn: sqlite3.Connection, graph_json: str) -> None:
    conn.execute(
        """INSERT INTO profile (id, graph_json, updated_at)
           VALUES (1, ?, CURRENT_TIMESTAMP)
           ON CONFLICT(id) DO UPDATE SET
             graph_json = excluded.graph_json,
             updated_at = CURRENT_TIMESTAMP""",
        (graph_json,),
    )
    conn.commit()


def load_profile(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute("SELECT graph_json FROM profile WHERE id = 1").fetchone()
    return json.loads(row["graph_json"]) if row else None


def upsert_jobs(conn: sqlite3.Connection, jobs: list[dict]) -> int:
    before = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    conn.executemany(
        """INSERT INTO jobs (external_id, source, company, title, location, url, description)
           VALUES (:external_id, :source, :company, :title, :location, :url, :description)
           ON CONFLICT(external_id) DO UPDATE SET
             title = excluded.title,
             description = excluded.description""",
        jobs,
    )
    conn.commit()
    after = conn.execute("SELECT COUNT(*) AS c FROM jobs").fetchone()["c"]
    return after - before


def save_fit_score(conn: sqlite3.Connection, external_id: str, score: dict) -> None:
    conn.execute(
        """INSERT INTO fit_scores
           (external_id, score, verdict, matched_skills, missing_skills, reasoning)
           VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(external_id) DO UPDATE SET
             score = excluded.score,
             verdict = excluded.verdict,
             matched_skills = excluded.matched_skills,
             missing_skills = excluded.missing_skills,
             reasoning = excluded.reasoning,
             scored_at = CURRENT_TIMESTAMP""",
        (
            external_id,
            score["score"],
            score["verdict"],
            json.dumps(score.get("matched_skills", [])),
            json.dumps(score.get("missing_skills", [])),
            score.get("reasoning", ""),
        ),
    )
    conn.commit()


def unscored_jobs(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT j.* FROM jobs j
           LEFT JOIN fit_scores f ON f.external_id = j.external_id
           WHERE f.external_id IS NULL
           LIMIT ?""",
        (limit,),
    ).fetchall()


def top_matches(conn: sqlite3.Connection, limit: int = 20) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT j.company, j.title, j.location, j.url,
                  f.score, f.verdict, f.matched_skills, f.missing_skills, f.reasoning
           FROM fit_scores f JOIN jobs j ON j.external_id = f.external_id
           ORDER BY f.score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
