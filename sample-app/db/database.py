"""Database access layer — minimal ORM-free helper."""

import os
from typing import Any, Dict, List, Optional

CANONICAL_COLUMNS = (
    '"id", "task_key", "status", "started_at", "finished_at", "exit_code", "log_tail"'
)

DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@db:5432/appdb")


def get_builds(conn) -> List[Dict[str, Any]]:
    cursor = conn.cursor()
    cursor.execute(f"SELECT {CANONICAL_COLUMNS} FROM builds ORDER BY id DESC LIMIT 100")
    cols = [d[0] for d in cursor.description]
    return [dict(zip(cols, row)) for row in cursor.fetchall()]


def insert_build(conn, task_key: str) -> int:
    cursor = conn.cursor()
    cursor.execute(
        "INSERT INTO builds (task_key, status) VALUES (%s, %s) RETURNING id",
        (task_key, "queued"),
    )
    conn.commit()
    return cursor.fetchone()[0]


def update_build(conn, build_id: int, **kwargs) -> None:
    if not kwargs:
        return
    sets = ", ".join(f"{k} = %s" for k in kwargs)
    cursor = conn.cursor()
    cursor.execute(
        f"UPDATE builds SET {sets} WHERE id = %s",
        (*kwargs.values(), build_id),
    )
    conn.commit()
