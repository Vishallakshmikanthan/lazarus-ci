-- 001_init.sql: initial schema for the CI/CD repair environment build log store.
-- Compatible with SQLite (default) and Postgres (DB_BACKEND=postgres).
-- NOTE: Postgres migration runner replaces AUTOINCREMENT and datetime('now') at load time.

CREATE TABLE IF NOT EXISTS builds (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    task_key     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending',
    started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    finished_at  TEXT,
    exit_code    INTEGER,
    log_tail     TEXT
);

CREATE INDEX IF NOT EXISTS idx_builds_task_key ON builds (task_key);
CREATE INDEX IF NOT EXISTS idx_builds_status   ON builds (status);
