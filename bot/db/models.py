SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    gid               TEXT UNIQUE,
    user_id           INTEGER NOT NULL,
    chat_id           INTEGER NOT NULL,
    reply_message_id  INTEGER,
    source_type       TEXT NOT NULL,      -- tg_media | url | magnet | torrent
    source_ref        TEXT,               -- file_unique_id or url hash
    file_name         TEXT,
    file_size         INTEGER,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    save_path         TEXT,
    error             TEXT,
    gofile_link       TEXT,
    payload           TEXT,               -- original source (url/magnet/file uri/torrent path) for retry

    created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    finished_at       TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_source
    ON tasks(source_type, source_ref)
    WHERE status = 'COMPLETED';

CREATE TABLE IF NOT EXISTS allowed_users (
    user_id     INTEGER PRIMARY KEY,
    note        TEXT,
    added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

# CREATE TABLE IF NOT EXISTS above only takes effect on a brand-new database — an
# already-deployed one needs these run explicitly. Each is safe to re-run; repo.py
# swallows the "duplicate column" error SQLite raises on a repeat.
MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN gofile_link TEXT",
    "ALTER TABLE tasks ADD COLUMN payload TEXT",
]

# Valid status values, kept here as the single source of truth for the state machine.
STATUSES = ("PENDING", "ACTIVE", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
