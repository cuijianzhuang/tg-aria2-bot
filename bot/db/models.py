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

-- 使用申请：没权限的人点「申请使用」后建一行，管理员点通过/拒绝后更新状态。
-- 一个人只有一行（重复申请复用同一行），所以"重复申请只算一次"天然成立。
CREATE TABLE IF NOT EXISTS access_requests (
    user_id       INTEGER PRIMARY KEY,
    username      TEXT,
    full_name     TEXT,
    status        TEXT NOT NULL DEFAULT 'pending',   -- pending | approved | rejected
    requested_at  REAL NOT NULL,
    notified_at   REAL,                              -- 上次成功通知到管理员的时间
    decided_at    REAL,
    decided_by    INTEGER
);

-- 普通用户的个别额度覆盖。NULL = 沿用 .env 里的默认值；0 = 不限制。
CREATE TABLE IF NOT EXISTS user_quotas (
    user_id         INTEGER PRIMARY KEY,
    max_active      INTEGER,    -- 同时进行的任务数
    max_task_bytes  INTEGER,    -- 单个任务大小上限
    daily_bytes     INTEGER     -- 每天下载总量
);

-- 每人每天实际下载的字节数（按轮询时看到的增量累加，跟任务创建在哪天无关，
-- 所以隔夜的任务不会绕过额度）
CREATE TABLE IF NOT EXISTS usage_daily (
    user_id  INTEGER NOT NULL,
    day      TEXT NOT NULL,       -- YYYY-MM-DD，服务器时区的自然日
    bytes    INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
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
    batch_id    TEXT,             -- 一条消息里贴多条链接时，同批的行共享这个 id
    node        TEXT NOT NULL DEFAULT 'default'   -- 目标节点，确认卡片创建时锁定
);

-- 额外的 aria2 节点（default 节点来自 .env，不入库、不可删）
CREATE TABLE IF NOT EXISTS nodes (
    name          TEXT PRIMARY KEY,
    rpc_url       TEXT NOT NULL,
    secret        TEXT NOT NULL,
    download_dir  TEXT NOT NULL DEFAULT '/downloads',
    is_local      INTEGER NOT NULL DEFAULT 0,   -- bot 是否与该节点同机（决定本地能力开关）
    enabled       INTEGER NOT NULL DEFAULT 1,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 用户偏好，目前只有"当前节点"；后续 per-user 偏好统一放这里
CREATE TABLE IF NOT EXISTS user_prefs (
    user_id       INTEGER PRIMARY KEY,
    current_node  TEXT NOT NULL DEFAULT 'default'
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
    # 多节点支持：旧行全部归 default 节点（升级前只有这一个节点，语义正确）
    "ALTER TABLE tasks ADD COLUMN node TEXT NOT NULL DEFAULT 'default'",
    "ALTER TABLE pending_tasks ADD COLUMN node TEXT NOT NULL DEFAULT 'default'",
    # 按用户过滤任务列表/统计上线后的高频查询：WHERE user_id = ? ORDER BY
    # created_at（列表/搜索）和 WHERE user_id = ? [AND status = ?]（计数/统计）
    "CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_tasks_user_status ON tasks(user_id, status)",
    # 白名单页显示昵称/用户名（通过申请加入的用户会带上这两项）
    "ALTER TABLE allowed_users ADD COLUMN username TEXT",
    "ALTER TABLE allowed_users ADD COLUMN full_name TEXT",
]

# Valid status values, kept here as the single source of truth for the state machine.
STATUSES = ("PENDING", "ACTIVE", "PAUSED", "COMPLETED", "FAILED", "CANCELLED")
