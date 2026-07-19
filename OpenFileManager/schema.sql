-- PostgreSQL schema for OpenFilesLogger
-- Run this inside the target database, for example: psql -d filelogger -f schema.sql

CREATE TABLE IF NOT EXISTS file_open_events (
    id              BIGSERIAL PRIMARY KEY,
    source          TEXT NOT NULL DEFAULT 'windows-security-4663',
    event_record_id BIGINT NOT NULL,
    host            TEXT NOT NULL,
    event_time      TIMESTAMPTZ NOT NULL,
    username        TEXT,
    domain          TEXT,
    process_name    TEXT,
    process_id      INTEGER,
    path            TEXT NOT NULL,
    extension       TEXT,
    file_category   TEXT NOT NULL DEFAULT 'other',
    access_mask     TEXT,
    access_list     TEXT,
    raw_event       JSONB NOT NULL,
    inserted_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_file_open_event UNIQUE (host, source, event_record_id)
);

CREATE INDEX IF NOT EXISTS idx_file_open_events_time
    ON file_open_events (event_time DESC);

CREATE INDEX IF NOT EXISTS idx_file_open_events_extension
    ON file_open_events (lower(extension));

CREATE INDEX IF NOT EXISTS idx_file_open_events_category_time
    ON file_open_events (file_category, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_file_open_events_user_time
    ON file_open_events (username, event_time DESC);

CREATE INDEX IF NOT EXISTS idx_file_open_events_path
    ON file_open_events (path);
