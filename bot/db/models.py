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

-- Unconfirmed "开始下载" cards. Used to be an in-memory dict, which meant a bot
-- restart silently expired every pending confirmation; persisting it lets a
-- restart mid-confirmation still resolve correctly.
CREATE TABLE IF NOT EXISTS pending_tasks (
    token       TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    user_id     INTEGER NOT NULL,
    chat_id     INTEGER NOT NULL,
    source_ref  TEXT,
    file_name   TEXT,
    file_size   INTEGER,
    payload     TEXT NOT NULL,
    created_at  REAL NOT NULL,
    batch_id    TEXT              -- 一条消息里贴多条链接时，同批的行共享这个 id
);
"""

# CREATE TABLE IF NOT EXISTS above only takes effect on a brand-new database — an
# already-deployed one needs these run explicitly. Each is safe to re-run; repo.py
# swallows the "duplicate column" error SQLite raises on a repeat.
MIGRATIONS = [
    "ALTER TABLE tasks ADD COLUMN gofile_link TEXT",
    "ALTER TABLE tasks ADD COLUMN payload TEXT",
    # hot query paths: WHERE status = ? (lists, counts, poll loop) and
    # ORDER BY created_at DESC (recent lists) — both full scans without these
    "CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_created ON tasks(created_at DESC)",
    "ALTER TABLE pending_tasks ADD COLUMN batch_id TEXT",
]

# Valid status values, kept here as the single source of truth for the state machine.
STATUSES = ("PENDING", "ACTIVE", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
