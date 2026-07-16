import sqlite3
from time import time
from uuid import uuid4

import aiosqlite
from datetime import datetime, timezone

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
    ) -> int:
        cur = await self._conn.execute(
            """
            INSERT INTO tasks (gid, user_id, chat_id, reply_message_id,
                                source_type, source_ref, file_name, file_size, payload, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
            """,
            (gid, user_id, chat_id, reply_message_id, source_type, source_ref, file_name, file_size, payload),
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

    async def update_gofile_link(self, gid: str, link: str):
        await self._conn.execute("UPDATE tasks SET gofile_link = ? WHERE gid = ?", (link, gid))
        await self._conn.commit()

    async def update_status(self, gid: str, status: str, *, error: str | None = None, save_path: str | None = None):
        finished_at = datetime.now(timezone.utc).isoformat() if status in ("COMPLETED", "FAILED", "CANCELLED") else None
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
        if status:
            cur = await self._conn.execute(
                "SELECT * FROM tasks WHERE status = ? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (status, limit, offset),
            )
        else:
            cur = await self._conn.execute(
                "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)
            )
        return await cur.fetchall()

    async def count_tasks(self, status: str | None = None) -> int:
        if status:
            cur = await self._conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status = ?", (status,))
        else:
            cur = await self._conn.execute("SELECT COUNT(*) AS n FROM tasks")
        row = await cur.fetchone()
        return row["n"]

    async def get_unfinished(self) -> list[aiosqlite.Row]:
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
    ) -> str:
        await self._cleanup_expired_pending()
        token = uuid4().hex[:12]
        await self._conn.execute(
            """
            INSERT INTO pending_tasks (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, time()),
        )
        await self._conn.commit()
        return token

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
                (token, kind, user_id, chat_id, source_ref, file_name, file_size, payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (pending.token, pending.kind, pending.user_id, pending.chat_id, pending.source_ref,
             pending.file_name, pending.file_size, pending.payload, pending.created_at),
        )
        await self._conn.commit()

    async def delete_pending(self, token: str):
        await self._conn.execute("DELETE FROM pending_tasks WHERE token = ?", (token,))
        await self._conn.commit()

    async def _cleanup_expired_pending(self):
        await self._conn.execute(
            "DELETE FROM pending_tasks WHERE created_at < ?", (time() - TTL_SECONDS,)
        )
        await self._conn.commit()
