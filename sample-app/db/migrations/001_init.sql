CREATE TABLE IF NOT EXISTS builds (
    id          SERIAL PRIMARY KEY,
    task_key    TEXT        NOT NULL,
    status      TEXT        NOT NULL DEFAULT 'queued',
    started_at  TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    exit_code   INTEGER,
    log_tail    TEXT
);
