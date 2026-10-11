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

    async def set_reply_message(self, task_id: int, message_id: int):
        await self._conn.execute("UPDATE tasks SET reply_message_id = ? WHERE id = ?", (message_id, task_id))
        await self._conn.commit()

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

    async def update_file_name(self, gid: str, file_name: str):
        """种子/磁力任务拿到元数据后，把真正的内容名回写（入库时只有 .torrent
        文件名或磁力链接），任务列表、搜索、完成通知都跟着显示好看的名字。"""
        await self._conn.execute("UPDATE tasks SET file_name = ? WHERE gid = ?", (file_name, gid))
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

    # ---------- 过期文件自动清理 ----------

    async def get_expiring(
        self, cutoff_iso: str, *, unwarned: bool = False, warned_before: float | None = None, limit: int = 200,
    ) -> list[aiosqlite.Row]:
        """已完成、文件还在、没被保留、完成时间早于 cutoff 的任务。
        unwarned=True 只取还没提醒过的；warned_before=T 只取在 T 之前就已经提醒过的
        （expiry_warned 存的是提醒时刻的 epoch 秒，0 = 没提醒过）。"""
        sql = (
            "SELECT * FROM tasks WHERE status = 'COMPLETED' AND files_deleted = 0 AND expiry_keep = 0 "
            "AND save_path IS NOT NULL AND finished_at IS NOT NULL AND finished_at < ?"
        )
        params: list = [cutoff_iso]
        if unwarned:
            sql += " AND expiry_warned = 0"
        if warned_before is not None:
            sql += " AND expiry_warned > 0 AND expiry_warned <= ?"
            params.append(warned_before)
        cur = await self._conn.execute(sql + " ORDER BY finished_at LIMIT ?", (*params, limit))
        return await cur.fetchall()

    async def mark_expiry_warned(self, task_id: int):
        await self._conn.execute("UPDATE tasks SET expiry_warned = ? WHERE id = ?", (time(), task_id))
        await self._conn.commit()

    async def mark_files_deleted(self, task_id: int):
        await self._conn.execute("UPDATE tasks SET files_deleted = 1 WHERE id = ?", (task_id,))
        await self._conn.commit()

    async def set_expiry_keep(self, task_id: int, keep: bool):
        await self._conn.execute("UPDATE tasks SET expiry_keep = ? WHERE id = ?", (1 if keep else 0, task_id))
        await self._conn.commit()

    # ---------- RSS 订阅 ----------

    async def add_feed(self, *, user_id: int, chat_id: int, url: str, keyword: str, exclude: str, node: str) -> int | None:
        """返回新订阅的 id；同一个人重复订阅同一个地址返回 None。"""
        try:
            cur = await self._conn.execute(
                "INSERT INTO rss_feeds (user_id, chat_id, url, keyword, exclude, node, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (user_id, chat_id, url, keyword, exclude, node, time()),
            )
        except sqlite3.IntegrityError:
            return None
        await self._conn.commit()
        return cur.lastrowid

    async def list_feeds(self, *, user_id: int | None = None, enabled_only: bool = False) -> list[aiosqlite.Row]:
        conds, params = [], []
        if user_id is not None:
            conds.append("user_id = ?")
            params.append(user_id)
        if enabled_only:
            conds.append("enabled = 1")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        cur = await self._conn.execute(f"SELECT * FROM rss_feeds{where} ORDER BY id", params)
        return await cur.fetchall()

    async def get_feed(self, feed_id: int) -> aiosqlite.Row | None:
        cur = await self._conn.execute("SELECT * FROM rss_feeds WHERE id = ?", (feed_id,))
        return await cur.fetchone()

    async def delete_feed(self, feed_id: int):
        await self._conn.execute("DELETE FROM rss_seen WHERE feed_id = ?", (feed_id,))
        await self._conn.execute("DELETE FROM rss_feeds WHERE id = ?", (feed_id,))
        await self._conn.commit()

    async def set_feed_enabled(self, feed_id: int, enabled: bool):
        await self._conn.execute("UPDATE rss_feeds SET enabled = ? WHERE id = ?", (1 if enabled else 0, feed_id))
        await self._conn.commit()

    async def update_feed_state(self, feed_id: int, *, title: str | None = None, error: str | None = None):
        await self._conn.execute(
            # 出错不更新 last_checked：它为空代表"还没成功基线过"，否则出错后的下一次
            # 成功轮询会被当成正常轮询，把源里的全部历史条目都下载一遍
            "UPDATE rss_feeds SET last_checked = COALESCE(?, last_checked), last_error = ?, "
            "title = COALESCE(?, title) WHERE id = ?",
            (time() if error is None else None, error, title, feed_id),
        )
        await self._conn.commit()

    async def seen_guids(self, feed_id: int) -> set[str]:
        cur = await self._conn.execute("SELECT guid FROM rss_seen WHERE feed_id = ?", (feed_id,))
        return {r["guid"] for r in await cur.fetchall()}

    async def mark_seen(self, feed_id: int, guids: list[str]):
        now = time()
        await self._conn.executemany(
            "INSERT OR IGNORE INTO rss_seen (feed_id, guid, seen_at) VALUES (?, ?, ?)",
            [(feed_id, g, now) for g in guids],
        )
        # 每个订阅只留最近 2000 条记录，免得无限增长
        await self._conn.execute(
            "DELETE FROM rss_seen WHERE feed_id = ? AND rowid NOT IN "
            "(SELECT rowid FROM rss_seen WHERE feed_id = ? ORDER BY seen_at DESC, rowid DESC LIMIT 2000)",
            (feed_id, feed_id),
        )
        await self._conn.commit()

    async def search_tasks(self, keyword: str, limit: int = 15, *, user_id: int | None = None) -> list[aiosqlite.Row]:
        """按文件名/来源标识模糊搜索，多取 1 条用来在渲染层判断"结果是否被截断"。
        SQLite 的 LIKE 对 ASCII 字母默认大小写不敏感，中文本身没有大小写问题。
        user_id=None 搜全部（管理员）；否则只搜这个用户自己发起的任务。"""
        pattern = f"%{keyword}%"
        where = "WHERE (file_name LIKE ? OR source_ref LIKE ?)"
        params: list = [pattern, pattern]
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)
        cur = await self._conn.execute(
            f"SELECT * FROM tasks {where} "
            # created_at 的 SQLite CURRENT_TIMESTAMP 精度只到秒，同一秒内插入
            # 的多条记录 ORDER BY created_at 排序不稳定；id 递增，用它做二级排序键
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, limit + 1),
        )
        return await cur.fetchall()

    async def get_period_stats(self, since: str | None, *, user_id: int | None = None) -> dict:
        """since 为 None 时统计全部时间；否则是 'YYYY-MM-DD HH:MM:SS' 格式的 UTC
        时间戳字符串 —— 必须匹配 SQLite CURRENT_TIMESTAMP 写入 created_at 时用的
        格式（不能传 ISO8601 的 T 分隔格式，字符串比较会因为格式不同而失真）。
        user_id=None 统计全部（管理员）；否则只统计这个用户自己的任务。"""
        conditions = []
        params: list = []
        if since:
            conditions.append("created_at >= ?")
            params.append(since)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
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
        self, limit: int = 10, offset: int = 0, status: str | None = None, *, user_id: int | None = None
    ) -> list[aiosqlite.Row]:
        # id DESC 作为 created_at（精度到秒）打平的二级排序键，避免同一秒内插入
        # 的多条记录顺序不稳定，翻页时行错位或重复
        # user_id=None 取全部用户的任务（管理员）；否则只取这个用户自己的
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur = await self._conn.execute(
            f"SELECT * FROM tasks {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        return await cur.fetchall()

    async def count_tasks(self, status: str | None = None, *, user_id: int | None = None) -> int:
        conditions = []
        params: list = []
        if status:
            conditions.append("status = ?")
            params.append(status)
        if user_id is not None:
            conditions.append("user_id = ?")
            params.append(user_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        cur = await self._conn.execute(f"SELECT COUNT(*) AS n FROM tasks {where}", params)
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

    async def count_by_status(self, *, user_id: int | None = None) -> dict[str, int]:
        """user_id=None 统计全部用户（管理员看的主页/列表 tab 计数）；
        否则只统计这个用户自己的任务。"""
        if user_id is not None:
            cur = await self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM tasks WHERE user_id = ? GROUP BY status", (user_id,)
            )
        else:
            cur = await self._conn.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status")
        rows = await cur.fetchall()
        return {row["status"]: row["n"] for row in rows}

    # ---- allowed_users: DB-managed whitelist additions on top of the env-seeded ALLOWED_USER_IDS ----

    async def add_allowed_user(
        self, user_id: int, note: str | None = None, *, username: str | None = None, full_name: str | None = None,
    ):
        # 重复添加时只覆盖传进来的非空字段，不会把已有的备注/用户名冲成空
        await self._conn.execute(
            """
            INSERT INTO allowed_users (user_id, note, username, full_name) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                note = COALESCE(excluded.note, note),
                username = COALESCE(excluded.username, username),
                full_name = COALESCE(excluded.full_name, full_name)
            """,
            (user_id, note, username, full_name),
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

    # ---- access_requests: 没权限的人点「申请使用」→ 管理员通过/拒绝 ----

    async def get_access_request(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self._conn.execute("SELECT * FROM access_requests WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    async def upsert_access_request(
        self, user_id: int, username: str | None, full_name: str | None, *, now: float | None = None,
    ):
        """新建（或重新发起）一条待处理的申请。"""
        await self._conn.execute(
            """
            INSERT INTO access_requests (user_id, username, full_name, status, requested_at)
            VALUES (?, ?, ?, 'pending', ?)
            ON CONFLICT(user_id) DO UPDATE SET
                username = excluded.username, full_name = excluded.full_name,
                status = 'pending', requested_at = excluded.requested_at,
                notified_at = NULL, decided_at = NULL, decided_by = NULL
            """,
            (user_id, username, full_name, time() if now is None else now),
        )
        await self._conn.commit()

    async def mark_request_notified(self, user_id: int, *, now: float | None = None):
        await self._conn.execute(
            "UPDATE access_requests SET notified_at = ? WHERE user_id = ?",
            (time() if now is None else now, user_id),
        )
        await self._conn.commit()

    async def decide_access_request(
        self, user_id: int, status: str, decided_by: int | None, *, now: float | None = None,
    ) -> bool:
        """只有还在 pending 的申请才能被处理——两个管理员同时点，只有先到的生效。
        返回这次调用是否真的改了状态。"""
        assert status in ("approved", "rejected")
        cur = await self._conn.execute(
            """
            UPDATE access_requests SET status = ?, decided_by = ?, decided_at = ?
            WHERE user_id = ? AND status = 'pending'
            """,
            (status, decided_by, time() if now is None else now, user_id),
        )
        await self._conn.commit()
        return cur.rowcount > 0

    async def list_pending_requests(self) -> list[aiosqlite.Row]:
        cur = await self._conn.execute(
            "SELECT * FROM access_requests WHERE status = 'pending' ORDER BY requested_at"
        )
        return await cur.fetchall()

    # ---- 额度：个别用户的覆盖值 + 每日用量 ----

    _QUOTA_FIELDS = ("max_active", "max_task_bytes", "daily_bytes")

    async def get_user_quota(self, user_id: int) -> aiosqlite.Row | None:
        cur = await self._conn.execute("SELECT * FROM user_quotas WHERE user_id = ?", (user_id,))
        return await cur.fetchone()

    async def set_user_quota(self, user_id: int, changes: dict[str, int | None]):
        """changes: 字段名 -> 值；None 表示清掉覆盖（回到 .env 默认值），0 表示不限。
        字段名走白名单，不会被拼进 SQL。"""
        bad = set(changes) - set(self._QUOTA_FIELDS)
        if bad:
            raise ValueError(f"unknown quota field(s): {sorted(bad)}")
        await self._conn.execute("INSERT OR IGNORE INTO user_quotas (user_id) VALUES (?)", (user_id,))
        for field, value in changes.items():
            await self._conn.execute(f"UPDATE user_quotas SET {field} = ? WHERE user_id = ?", (value, user_id))
        await self._conn.commit()

    async def clear_user_quota(self, user_id: int):
        await self._conn.execute("DELETE FROM user_quotas WHERE user_id = ?", (user_id,))
        await self._conn.commit()

    async def count_active_by_user(self, user_id: int) -> int:
        """进行中的任务数：排队 + 下载 + 暂停（还占着名额的）。"""
        cur = await self._conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE user_id = ? AND status IN ('PENDING', 'ACTIVE', 'PAUSED')",
            (user_id,),
        )
        return (await cur.fetchone())[0]

    async def add_usage(self, user_id: int, day: str, delta: int):
        await self._conn.execute(
            """
            INSERT INTO usage_daily (user_id, day, bytes) VALUES (?, ?, ?)
            ON CONFLICT(user_id, day) DO UPDATE SET bytes = bytes + excluded.bytes
            """,
            (user_id, day, delta),
        )
        await self._conn.commit()

    async def get_usage(self, user_id: int, day: str) -> int:
        cur = await self._conn.execute(
            "SELECT bytes FROM usage_daily WHERE user_id = ? AND day = ?", (user_id, day)
        )
        row = await cur.fetchone()
        return row[0] if row else 0

    async def get_usage_since(self, since_day: str, *, user_id: int | None = None) -> list[tuple[str, int]]:
        """[(day, bytes)] 按天汇总；user_id=None 汇总所有人。"""
        sql, params = "SELECT day, SUM(bytes) FROM usage_daily WHERE day >= ?", [since_day]
        if user_id is not None:
            sql += " AND user_id = ?"
            params.append(user_id)
        cur = await self._conn.execute(sql + " GROUP BY day ORDER BY day", params)
        return [(r[0], r[1] or 0) for r in await cur.fetchall()]

    async def get_top_users_since(self, since_day: str, limit: int = 5) -> list[tuple[int, int]]:
        cur = await self._conn.execute(
            "SELECT user_id, SUM(bytes) AS b FROM usage_daily WHERE day >= ? GROUP BY user_id "
            "HAVING b > 0 ORDER BY b DESC LIMIT ?", (since_day, limit),
        )
        return [(r[0], r[1]) for r in await cur.fetchall()]

    async def prune_usage(self, before_day: str) -> int:
        cur = await self._conn.execute("DELETE FROM usage_daily WHERE day < ?", (before_day,))
        await self._conn.commit()
        return cur.rowcount

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
