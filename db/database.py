"""Database service layer — SQLite backend.

Fault flags — each defaults to off; set to "true" to activate:
    BAD_MIGRATION_SQL    Replaces CREATE TABLE with CREAT TABLE in the migration SQL at load time.
    SCHEMA_DRIFT         Adds a phantom column to EXPECTED_COLUMNS that the table never received.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
from pathlib import Path
from typing import Generator, List

# ---------------------------------------------------------------------------
# URL resolution
# ---------------------------------------------------------------------------

DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///./app.db")

# ---------------------------------------------------------------------------
# Schema definition
# SCHEMA_DRIFT adds a phantom column that was never added to the real table.
# ---------------------------------------------------------------------------

CANONICAL_COLUMNS: List[str] = [
    "id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail",
]

_schema_drift_active = os.environ.get("SCHEMA_DRIFT") == "true"
EXPECTED_COLUMNS: List[str] = (
    CANONICAL_COLUMNS + ["artifact_url"] if _schema_drift_active else list(CANONICAL_COLUMNS)
)

# ---------------------------------------------------------------------------
# SQLite connection helper
# ---------------------------------------------------------------------------

_MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def _sqlite_path() -> Path:
    return Path(DATABASE_URL.removeprefix("sqlite:///"))


@contextlib.contextmanager
def get_db() -> Generator[sqlite3.Connection, None, None]:
    """Yield a committed-or-rolled-back SQLite connection."""
    path = _sqlite_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration runner
# ---------------------------------------------------------------------------

def _load_sql(filename: str) -> str:
    sql = (_MIGRATIONS_DIR / filename).read_text(encoding="utf-8")
    if os.environ.get("BAD_MIGRATION_SQL") == "true":
        sql = sql.replace("CREATE TABLE IF NOT EXISTS builds", "CREAT TABLE IF NOT EXISTS builds", 1)
    return sql


def init_db() -> None:
    """Apply all pending SQL migrations. Idempotent — safe to call on every startup."""
    if not DATABASE_URL.startswith("sqlite:"):
        raise ValueError(f"Unsupported DATABASE_URL scheme: {DATABASE_URL!r}")
    with get_db() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations "
            "(name TEXT PRIMARY KEY, applied_at TEXT DEFAULT (datetime('now')))"
        )
        for path in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            name = path.name
            if conn.execute("SELECT 1 FROM _migrations WHERE name=?", (name,)).fetchone():
                continue
            conn.executescript(_load_sql(name))
            conn.execute("INSERT INTO _migrations (name) VALUES (?)", (name,))


# ---------------------------------------------------------------------------
# CRUD helpers
# ---------------------------------------------------------------------------

def insert_build(task_key: str) -> int:
    """Insert a pending build row and return its id."""
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO builds (task_key, status) VALUES (?, 'pending')", (task_key,)
        )
        return cur.lastrowid  # type: ignore[return-value]


def update_build(build_id: int, *, status: str, exit_code: int, log_tail: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE builds SET status=?, exit_code=?, log_tail=?, finished_at=datetime('now') "
            "WHERE id=?",
            (status, exit_code, log_tail, build_id),
        )


def get_recent_builds(task_key: str, limit: int = 10) -> list[sqlite3.Row]:
    with get_db() as conn:
        return conn.execute(
            "SELECT * FROM builds WHERE task_key=? ORDER BY started_at DESC LIMIT ?",
            (task_key, limit),
        ).fetchall()
