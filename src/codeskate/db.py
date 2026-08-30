"""SQLite persistence. No Postgres needed until you have real users.

`applications` is the spine of the system: every agent from Phase 1 onward reads
or advances a row here. That is what turns 18 agents into one pipeline instead of
18 disconnected scripts.
"""

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

-- The pipeline. One row per job you decided to pursue.
CREATE TABLE IF NOT EXISTS applications (
    external_id       TEXT PRIMARY KEY REFERENCES jobs(external_id),
    stage             TEXT NOT NULL DEFAULT 'shortlisted',
    stage_entered_at  TEXT DEFAULT CURRENT_TIMESTAMP,
    created_at        TEXT DEFAULT CURRENT_TIMESTAMP,
    notes             TEXT DEFAULT ''
);

-- Generated documents: tailored resumes, cover letters, prep briefs, plans.
CREATE TABLE IF NOT EXISTS artifacts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id  TEXT,
    kind         TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at   TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company_intel (
    company    TEXT PRIMARY KEY,
    intel_json TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS comp_estimates (
    external_id   TEXT PRIMARY KEY REFERENCES jobs(external_id),
    estimate_json TEXT NOT NULL,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS gap_reports (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    target_role TEXT NOT NULL,
    report_json TEXT NOT NULL,
    created_at  TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mock_sessions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id    TEXT,
    kind           TEXT NOT NULL,
    transcript_json TEXT NOT NULL,
    avg_score      REAL,
    created_at     TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Ground truth for the learning loop. Without this the system cannot know
-- whether it is working or you just got lucky.
CREATE TABLE IF NOT EXISTS outcomes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    external_id TEXT,
    event       TEXT NOT NULL,
    detail      TEXT DEFAULT '',
    occurred_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_app_stage ON applications(stage);
CREATE INDEX IF NOT EXISTS idx_artifact_kind ON artifacts(external_id, kind);
"""


def connect() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


# --------------------------------------------------------------------------- #
# spend
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# profile
# --------------------------------------------------------------------------- #


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


# --------------------------------------------------------------------------- #
# jobs + scores
# --------------------------------------------------------------------------- #


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


def job_by_id(conn: sqlite3.Connection, external_id: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM jobs WHERE external_id = ?", (external_id,)).fetchone()


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


def top_matches(conn: sqlite3.Connection, limit: int = 20, min_score: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        """SELECT j.external_id, j.company, j.title, j.location, j.url,
                  f.score, f.verdict, f.matched_skills, f.missing_skills, f.reasoning,
                  a.stage
           FROM fit_scores f
           JOIN jobs j ON j.external_id = f.external_id
           LEFT JOIN applications a ON a.external_id = f.external_id
           WHERE f.score >= ?
           ORDER BY f.score DESC
           LIMIT ?""",
        (min_score, limit),
    ).fetchall()


# --------------------------------------------------------------------------- #
# applications (the pipeline)
# --------------------------------------------------------------------------- #


def add_application(conn: sqlite3.Connection, external_id: str, stage: str = "shortlisted") -> bool:
    """Returns True if newly added."""
    cur = conn.execute(
        """INSERT INTO applications (external_id, stage) VALUES (?, ?)
           ON CONFLICT(external_id) DO NOTHING""",
        (external_id, stage),
    )
    conn.commit()
    return cur.rowcount > 0


def set_stage(conn: sqlite3.Connection, external_id: str, stage: str, note: str = "") -> None:
    conn.execute(
        """UPDATE applications
           SET stage = ?, stage_entered_at = CURRENT_TIMESTAMP,
               notes = CASE WHEN ? = '' THEN notes ELSE notes || ? || char(10) END
           WHERE external_id = ?""",
        (stage, note, note, external_id),
    )
    conn.commit()


def get_application(conn: sqlite3.Connection, external_id: str) -> sqlite3.Row | None:
    return conn.execute(
        """SELECT a.*, j.company, j.title, j.url, j.location, j.description, f.score, f.verdict
           FROM applications a
           JOIN jobs j ON j.external_id = a.external_id
           LEFT JOIN fit_scores f ON f.external_id = a.external_id
           WHERE a.external_id = ?""",
        (external_id,),
    ).fetchone()


def list_applications(
    conn: sqlite3.Connection, stage: str | None = None
) -> list[sqlite3.Row]:
    sql = """SELECT a.*, j.company, j.title, j.url, j.location, f.score,
                    CAST(julianday('now') - julianday(a.stage_entered_at) AS INTEGER) AS days_in_stage
             FROM applications a
             JOIN jobs j ON j.external_id = a.external_id
             LEFT JOIN fit_scores f ON f.external_id = a.external_id"""
    params: tuple = ()
    if stage:
        sql += " WHERE a.stage = ?"
        params = (stage,)
    sql += " ORDER BY f.score DESC NULLS LAST, a.created_at"
    return conn.execute(sql, params).fetchall()


def stage_counts(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT stage, COUNT(*) AS c FROM applications GROUP BY stage"
    ).fetchall()
    return {r["stage"]: r["c"] for r in rows}


# --------------------------------------------------------------------------- #
# artifacts
# --------------------------------------------------------------------------- #


def save_artifact(
    conn: sqlite3.Connection, kind: str, payload_json: str, external_id: str | None = None
) -> int:
    cur = conn.execute(
        "INSERT INTO artifacts (external_id, kind, payload_json) VALUES (?, ?, ?)",
        (external_id, kind, payload_json),
    )
    conn.commit()
    return int(cur.lastrowid or 0)


def latest_artifact(
    conn: sqlite3.Connection, kind: str, external_id: str | None = None
) -> dict | None:
    if external_id:
        row = conn.execute(
            """SELECT payload_json FROM artifacts
               WHERE kind = ? AND external_id = ?
               ORDER BY id DESC LIMIT 1""",
            (kind, external_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT payload_json FROM artifacts WHERE kind = ? ORDER BY id DESC LIMIT 1",
            (kind,),
        ).fetchone()
    return json.loads(row["payload_json"]) if row else None


def has_artifact(conn: sqlite3.Connection, kind: str, external_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM artifacts WHERE kind = ? AND external_id = ? LIMIT 1",
        (kind, external_id),
    ).fetchone()
    return row is not None


# --------------------------------------------------------------------------- #
# intel, comp, gaps, mocks, outcomes
# --------------------------------------------------------------------------- #


def save_company_intel(conn: sqlite3.Connection, company: str, intel_json: str) -> None:
    conn.execute(
        """INSERT INTO company_intel (company, intel_json) VALUES (?, ?)
           ON CONFLICT(company) DO UPDATE SET
             intel_json = excluded.intel_json, created_at = CURRENT_TIMESTAMP""",
        (company, intel_json),
    )
    conn.commit()


def load_company_intel(conn: sqlite3.Connection, company: str) -> dict | None:
    row = conn.execute(
        "SELECT intel_json FROM company_intel WHERE company = ?", (company,)
    ).fetchone()
    return json.loads(row["intel_json"]) if row else None


def save_comp_estimate(conn: sqlite3.Connection, external_id: str, estimate_json: str) -> None:
    conn.execute(
        """INSERT INTO comp_estimates (external_id, estimate_json) VALUES (?, ?)
           ON CONFLICT(external_id) DO UPDATE SET
             estimate_json = excluded.estimate_json, created_at = CURRENT_TIMESTAMP""",
        (external_id, estimate_json),
    )
    conn.commit()


def load_comp_estimate(conn: sqlite3.Connection, external_id: str) -> dict | None:
    row = conn.execute(
        "SELECT estimate_json FROM comp_estimates WHERE external_id = ?", (external_id,)
    ).fetchone()
    return json.loads(row["estimate_json"]) if row else None


def save_gap_report(conn: sqlite3.Connection, target_role: str, report_json: str) -> None:
    conn.execute(
        "INSERT INTO gap_reports (target_role, report_json) VALUES (?, ?)",
        (target_role, report_json),
    )
    conn.commit()


def latest_gap_report(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        "SELECT report_json FROM gap_reports ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return json.loads(row["report_json"]) if row else None


def save_mock_session(
    conn: sqlite3.Connection,
    kind: str,
    transcript_json: str,
    avg_score: float,
    external_id: str | None = None,
) -> None:
    conn.execute(
        """INSERT INTO mock_sessions (external_id, kind, transcript_json, avg_score)
           VALUES (?, ?, ?, ?)""",
        (external_id, kind, transcript_json, avg_score),
    )
    conn.commit()


def record_outcome(
    conn: sqlite3.Connection, event: str, detail: str = "", external_id: str | None = None
) -> None:
    conn.execute(
        "INSERT INTO outcomes (external_id, event, detail) VALUES (?, ?, ?)",
        (external_id, event, detail),
    )
    conn.commit()


def outcome_summary(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT event, COUNT(*) AS c FROM outcomes GROUP BY event ORDER BY c DESC"
    ).fetchall()
