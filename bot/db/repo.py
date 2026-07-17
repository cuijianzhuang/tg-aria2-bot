import sqlite3
from datetime import UTC, datetime
from time import time
from uuid import uuid4

import aiosqlite

from bot.core.pending_tasks import TTL_SECONDS, PendingTask
from bot.db.models import MIGRATIONS, SCHEMA


class TaskRepo:
    def __init__(self, db_path: str):
        self._db_path = db_path
        self._conn: aiosqlite.Connection | None = None

    async def connect(self):
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        # bot and web are two separate processes sharing this file; WAL lets a
        # reader and a writer coexist, busy_timeout retries instead of raising
        # "database is locked" when both write at once.
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA busy_timeout=5000")
        await self._conn.execute("PRAGMA synchronous=NORMAL")
        await self._conn.executescript(SCHEMA)
        for migration in MIGRATIONS:
            try:
                await self._conn.execute(migration)
            except sqlite3.OperationalError as e:
                if "duplicate column name" not in str(e):
                    raise
        await self._conn.commit()

    async def close(self):
        if self._conn:
            await self._conn.close()

    async def create_task(
        self,
        *,
        gid: str,
        user_id: int,
        chat_id: int,
        reply_message_id: int | None,
        source_type: str,
        source_ref: str | None,
        file_name: str | None,
        file_size: int | None,
        payload: str | None = None,
        node: str = "default",
    ) -> int:
        cur = await self._conn.execute(
            """
            INSERT INTO tasks (gid, user_id, chat_id, reply_message_id,
                                source_type, source_ref, file_name, file_size, payload, node, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (gid, user_id, chat_id, reply_message_id, source_type, source_ref, file_name, file_size, payload, node),
        )
        await self._conn.commit()
        return cur.lastrowid

    async def retry_task(self, task_id: int, new_gid: str, *, reply_message_id: int | None = None):
        """Re-arm a failed/cancelled/completed task with a freshly-added aria2 gid.
        reply_message_id repoints progress edits at the message the retry came from."""
        await self._conn.execute(
            """
            UPDATE tasks
            SET gid = ?, status = 'PENDING', error = NULL, finished_at = NULL,
                reply_message_id = COALESCE(?, reply_message_id)
            WHERE id = ?
            """,
            (new_gid, reply_message_id, task_id),
        )
        await self._conn.commit()

    async def get_by_gid(self, gid: str) -> aiosqlite.Row | None:
        cur = await self._conn.execute("SELECT * FROM tasks WHERE gid = ?", (gid,))
        return await cur.fetchone()

    async def get_by_id(self, task_id: int) -> aiosqlite.Row | None:
        cur = await self._conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,))
        return await cur.fetchone()

    async def get_completed_by_source(self, source_type: str, source_ref: str) -> aiosqlite.Row | None:
        cur = await self._conn.execute(
            "SELECT * FROM tasks WHERE source_type = ? AND source_ref = ? AND status = 'COMPLETED'",
            (source_type, source_ref),
        )
        return await cur.fetchone()

    async def delete_task(self, gid: str):
        """Removes the task record only — never touches the downloaded file on disk."""
        await self._conn.execute("DELETE FROM tasks WHERE gid = ?", (gid,))
        await self._conn.commit()

    async def delete_by_status(self, status: str) -> int:
        cur = await self._conn.execute("DELETE FROM tasks WHERE status = ?", (status,))
        await self._conn.commit()
        return cur.rowcount

    async def delete_old_completed(self, cutoff_iso: str) -> int:
        """自动清理：删除 finished_at 早于 cutoff_iso 的已完成任务记录。
        只删数据库记录，磁盘上的文件不受影响。cutoff_iso 与 update_status 里
        写入的 finished_at 同为 UTC ISO8601，格式一致所以可以直接字符串比较。"""
        cur = await self._conn.execute(
            "DELETE FROM tasks WHERE status = 'COMPLETED' AND finished_at IS NOT NULL AND finished_at < ?",
            (cutoff_iso,),
        )
        await self._conn.commit()
        return cur.rowcount

    async def search_tasks(self, keyword: str, limit: int = 15) -> list[aiosqlite.Row]:
        """按文件名/来源标识模糊搜索，多取 1 条用来在渲染层判断"结果是否被截断"。
        SQLite 的 LIKE 对 ASCII 字母默认大小写不敏感，中文本身没有大小写问题。"""
        pattern = f"%{keyword}%"
        cur = await self._conn.execute(
            "SELECT * FROM tasks WHERE file_name LIKE ? OR source_ref LIKE ? "
            # created_at 的 SQLite CURRENT_TIMESTAMP 精度只到秒，同一秒内插入
            # 的多条记录 ORDER BY created_at 排序不稳定；id 递增，用它做二级排序键
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (pattern, pattern, limit + 1),
        )
        return await cur.fetchall()

    async def get_period_stats(self, since: str | None) -> dict:
        """since 为 None 时统计全部时间；否则是 'YYYY-MM-DD HH:MM:SS' 格式的 UTC
        时间戳字符串 —— 必须匹配 SQLite CURRENT_TIMESTAMP 写入 created_at 时用的
        格式（不能传 ISO8601 的 T 分隔格式，字符串比较会因为格式不同而失真）。"""
        where = "WHERE created_at >= ?" if since else ""
        params = (since,) if since else ()
        cur = await self._conn.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                SUM(CASE WHEN status = 'COMPLETED' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'FAILED' THEN 1 ELSE 0 END) AS failed,
                SUM(CASE WHEN status = 'CANCELLED' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status = 'COMPLETED' THEN file_size ELSE 0 END) AS total_bytes
            FROM tasks {where}
            """,
            params,
        )
        row = await cur.fetchone()
        return {
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "cancelled": row["cancelled"] or 0,
            "total_bytes": row["total_bytes"] or 0,
        }

    async def update_gofile_link(self, gid: str, link: str):
        await self._conn.execute("UPDATE tasks SET gofile_link = ? WHERE gid = ?", (link, gid))
        await self._conn.commit()

    async def update_status(self, gid: str, status: str, *, error: str | None = None, save_path: str | None = None):
        finished_at = datetime.now(UTC).isoformat() if status in ("COMPLETED", "FAILED", "CANCELLED") else None
        await self._conn.execute(
            """
            UPDATE tasks
            SET status = ?, error = COALESCE(?, error), save_path = COALESCE(?, save_path),
                finished_at = COALESCE(?, finished_at)
            WHERE gid = ?
            """,
            (status, error, save_path, finished_at, gid),
        )
        await self._conn.commit()

    async def list_recent(
        self, limit: int = 10, offset: int = 0, status: str | None = None
    ) -> list[aiosqlite.Row]:
        # id DESC 作为 created_at（精度到秒）打平的二级排序键，避免同一秒内插入
        # 的多条记录顺序不稳定，翻页时行错位或重复
        if status:
            cur = await self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        return await cur.fetchall()

    async def count_tasks(self, status: str | None = None) -> int:
        if status:
            cur = await self._conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = ?", (status,))
        else:
            cur = await self._conn.execute("SELECT COUNT(*) AS n FROM tasks")
        row = await cur.fetchone()
        return row["n"]

    async def get_unfinished(self, node: str | None = None) -> list[aiosqlite.Row]:
        """node=None 取全部节点的未完成任务；轮询循环按节点分别取，
        避免拿 A 节点的下载列表去比对 B 节点的任务（gid 对不上会被误判丢失）。"""
        if node:
            cur = await self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('PENDING', 'ACTIVE', 'PAUSED') AND node = ?",
                (node,),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('PENDING', 'ACTIVE', 'PAUSED')"
            )
        return await cur.fetchall()

    async def count_by_status(self) -> dict[str, int]:
        cur = await self._conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        rows = await cur.fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ---- allowed_users: DB-managed whitelist additions on top of the env-seeded ALLOWED_USER_IDS ----

    async def add_allowed_user(self, user_id: int, note: str | None = None):
        await self._conn.execute(
            "INSERT OR REPLACE INTO allowed_users (user_id, note) VALUES (?, ?)", (user_id, note)
        )
        await self._conn.commit()

    async def remove_allowed_user(self, user_id: int):
        await self._conn.execute("DELETE FROM allowed_users WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def list_allowed_users(self) -> list[aiosqlite.Row]:
        cur = await self._conn.execute("SELECT * FROM allowed_users ORDER BY added_at DESC")
        return await cur.fetchall()

    async def is_user_allowed(self, user_id: int) -> bool:
        cur = await self._conn.execute("SELECT 1 FROM allowed_users WHERE user_id = ?", (user_id,))
        return await cur.fetchone() is not None

    # ---- pending_tasks: unconfirmed "开始下载" cards, persisted so a bot restart
    # mid-confirmation doesn't just silently expire them ----

    async def create_pending(
        self,
        *,
        kind: str,
        user_id: int,
        chat_id: int,
        source_ref: str | None,
        file_name: str | None,
        file_size: int | None,
        payload: str,
        batch_id: str | None = None,
        node: str = "default",
    ) -> str:
        await self._cleanup_expired_pending()
        token = uuid4().hex[:12]
        await self._conn.execute(
            """
            INSERT INTO pending_tasks
                (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, created_at, batch_id, node)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, time(), batch_id, node),
        )
        await self._conn.commit()
        return token

    async def update_pending_node(self, token: str, node: str):
        """确认卡片上的"切换节点"：只改这一条待确认任务的目标节点。"""
        await self._conn.execute("UPDATE pending_tasks SET node = ? WHERE token = ?", (node, token))
        await self._conn.commit()

    async def get_pending(self, token: str) -> PendingTask | None:
        await self._cleanup_expired_pending()
        cur = await self._conn.execute("SELECT * FROM pending_tasks WHERE token = ?", (token,))
        row = await cur.fetchone()
        return PendingTask.from_row(row) if row else None

    async def pop_pending(self, token: str) -> PendingTask | None:
        """Atomically claim a pending confirmation: two rapid taps on 开始下载
        must not both see the row and add the download twice."""
        await self._cleanup_expired_pending()
        try:
            cur = await self._conn.execute(
                "DELETE FROM pending_tasks WHERE token = ? RETURNING *", (token,)
            )
            row = await cur.fetchone()
            await self._conn.commit()
            return PendingTask.from_row(row) if row else None
        except sqlite3.OperationalError:
            # RETURNING needs sqlite >= 3.35; fall back to the non-atomic path
            pending = await self.get_pending(token)
            if pending:
                await self.delete_pending(token)
            return pending

    async def restore_pending(self, pending: PendingTask):
        """Put a popped confirmation back (aria2 add failed — keep the button alive)."""
        await self._conn.execute(
            """
            INSERT OR REPLACE INTO pending_tasks
                (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, created_at, batch_id, node)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pending.token, pending.kind, pending.user_id, pending.chat_id, pending.source_ref,
             pending.file_name, pending.file_size, pending.payload, pending.created_at,
             pending.batch_id, pending.node),
        )
        await self._conn.commit()

    async def delete_pending(self, token: str):
        await self._conn.execute("DELETE FROM pending_tasks WHERE token = ?", (token,))
        await self._conn.commit()

    async def get_pending_batch(self, batch_id: str) -> list[PendingTask]:
        """一条"批量发送链接"消息生成的所有待确认任务，按创建顺序排列。"""
        await self._cleanup_expired_pending()
        cur = await self._conn.execute(
            "SELECT * FROM pending_tasks WHERE batch_id = ? ORDER BY created_at", (batch_id,)
        )
        rows = await cur.fetchall()
        return [PendingTask.from_row(row) for row in rows]

    async def delete_pending_batch(self, batch_id: str) -> int:
        cur = await self._conn.execute("DELETE FROM pending_tasks WHERE batch_id = ?", (batch_id,))
        await self._conn.commit()
        return cur.rowcount

    async def _cleanup_expired_pending(self):
        await self._conn.execute(
            "DELETE FROM pending_tasks WHERE created_at < ?", (time() - TTL_SECONDS,)
        )
        await self._conn.commit()

    # ---- nodes: 额外 aria2 节点注册表（default 节点来自 .env，不在这张表里） ----

    async def list_nodes(self) -> list[aiosqlite.Row]:
        cur = await self._conn.execute("SELECT * FROM nodes ORDER BY added_at")
        return await cur.fetchall()

    async def add_node(self, *, name: str, rpc_url: str, secret: str, download_dir: str, is_local: bool = False):
        await self._conn.execute(
            """
            INSERT INTO nodes (name, rpc_url, secret, download_dir, is_local, enabled)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (name, rpc_url, secret, download_dir, int(is_local)),
        )
        await self._conn.commit()

    async def delete_node(self, name: str):
        """只删注册表；该节点的历史任务记录保留（node 列还指向它，仅作展示）。"""
        await self._conn.execute("DELETE FROM nodes WHERE name = ?", (name,))
        await self._conn.commit()

    async def set_node_enabled(self, name: str, enabled: bool):
        await self._conn.execute("UPDATE nodes SET enabled = ? WHERE name = ?", (int(enabled), name))
        await self._conn.commit()

    # ---- user_prefs: per-user 当前节点 ----

    async def get_current_node(self, user_id: int) -> str:
        cur = await self._conn.execute(
            "SELECT current_node FROM user_prefs WHERE user_id = ?", (user_id,)
        )
        row = await cur.fetchone()
        return row["current_node"] if row else "default"

    async def set_current_node(self, user_id: int, node: str):
        await self._conn.execute(
            "INSERT OR REPLACE INTO user_prefs (user_id, current_node) VALUES (?, ?)",
            (user_id, node),
        )
        await self._conn.commit()
