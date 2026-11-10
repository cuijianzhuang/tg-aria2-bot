"""Video persistence; SQL stays in the database layer."""

import hashlib
import json
import secrets
import time
from uuid import uuid4

VIDEO_SCHEMA = """
CREATE TABLE IF NOT EXISTS video_jobs (
 task_id INTEGER PRIMARY KEY, parser TEXT NOT NULL, quality TEXT NOT NULL,
 account_mode TEXT NOT NULL, phase TEXT NOT NULL DEFAULT 'queued',
 completed INTEGER NOT NULL DEFAULT 0, total INTEGER NOT NULL DEFAULT 0,
 command TEXT, work_dir TEXT, output TEXT, progress_data TEXT NOT NULL DEFAULT '{}'
);
CREATE TRIGGER IF NOT EXISTS video_job_cleanup AFTER DELETE ON tasks BEGIN
 DELETE FROM video_jobs WHERE task_id=OLD.id;
END;
CREATE TABLE IF NOT EXISTS video_previews (
 token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
 url TEXT NOT NULL, parser TEXT NOT NULL, quality TEXT NOT NULL,
 account_mode TEXT NOT NULL, info TEXT NOT NULL, expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS video_accounts (
 platform TEXT NOT NULL, owner_id INTEGER NOT NULL,
 identity TEXT NOT NULL, cookies BLOB NOT NULL, enabled INTEGER NOT NULL DEFAULT 0,
 updated REAL NOT NULL, PRIMARY KEY(platform, owner_id)
);
CREATE TABLE IF NOT EXISTS video_preferences (
 user_id INTEGER NOT NULL, platform TEXT NOT NULL, mode TEXT NOT NULL,
 PRIMARY KEY(user_id,platform)
);
CREATE TABLE IF NOT EXISTS video_logins (
 id TEXT PRIMARY KEY, platform TEXT NOT NULL, owner_id INTEGER NOT NULL,
 requester_id INTEGER NOT NULL, state TEXT NOT NULL, expires REAL NOT NULL,
 qr_path TEXT, identity TEXT, chat_id INTEGER, message_id INTEGER,
 notified_state TEXT
);
CREATE TABLE IF NOT EXISTS video_web_grants (
 token_hash TEXT PRIMARY KEY, login_id TEXT NOT NULL, expires REAL NOT NULL,
 consumed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS video_web_sessions (
 token_hash TEXT PRIMARY KEY, login_id TEXT NOT NULL, expires REAL NOT NULL
);
"""


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class VideoRepoMixin:
    async def video_preview(self, token, user_id):
        cur = await self._conn.execute(
            "SELECT * FROM video_previews WHERE token=? AND user_id=? AND expires>?",
            (token, user_id, time.time()),
        )
        return await cur.fetchone()

    async def video_save_preview(self, *, user_id, chat_id, url, parser, quality, account_mode, info):
        token = secrets.token_hex(6)
        await self._conn.execute("DELETE FROM video_previews WHERE expires<?", (time.time(),))
        await self._conn.execute(
            "INSERT INTO video_previews VALUES (?,?,?,?,?,?,?,?,?)",
            (token, user_id, chat_id, url, parser, quality, account_mode, json.dumps(info), time.time() + 600),
        )
        await self._conn.commit()
        return token

    async def video_discard_preview(self, token, user_id):
        await self._conn.execute("DELETE FROM video_previews WHERE token=? AND user_id=?", (token, user_id))
        await self._conn.commit()

    async def video_create_task(self, token, user_id, message_id):
        # Claim and creation happen without a commit in between. A second caller
        # cannot claim the same preview, including across bot/web connections.
        cur = await self._conn.execute(
            "DELETE FROM video_previews WHERE token=? AND user_id=? AND expires>? RETURNING *",
            (token, user_id, time.time()),
        )
        preview = await cur.fetchone()
        if preview is None:
            await self._conn.commit()
            return None
        info = json.loads(preview["info"])
        gid = "video_" + uuid4().hex[:20]
        cur = await self._conn.execute(
            """INSERT INTO tasks (gid,user_id,chat_id,reply_message_id,source_type,source_ref,
               file_name,file_size,payload,node,status,backend)
               VALUES (?,?,?,?,'video',?,?,?,?, 'default','PENDING','video')""",
            (
                gid,
                user_id,
                preview["chat_id"],
                message_id,
                token_hash(preview["url"]),
                info["title"],
                info.get("size"),
                preview["url"],
            ),
        )
        task_id = cur.lastrowid
        await self._conn.execute(
            "INSERT INTO video_jobs (task_id,parser,quality,account_mode) VALUES (?,?,?,?)",
            (task_id, preview["parser"], preview["quality"], preview["account_mode"]),
        )
        await self._conn.commit()
        return await self.get_by_id(task_id)

    async def video_job(self, task_id):
        cur = await self._conn.execute("SELECT * FROM video_jobs WHERE task_id=?", (task_id,))
        return await cur.fetchone()

    async def video_jobs(self):
        cur = await self._conn.execute(
            """SELECT t.*,v.parser,v.quality,v.account_mode,v.phase,v.completed,v.total,
               v.command,v.work_dir,v.output,v.progress_data FROM tasks t JOIN video_jobs v ON t.id=v.task_id
               WHERE t.status IN ('PENDING','ACTIVE','PAUSED') OR v.command IS NOT NULL"""
        )
        return await cur.fetchall()

    async def video_update(self, task_id, **fields):
        allowed = {"phase", "completed", "total", "command", "work_dir", "output", "parser", "progress_data"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid video job fields")
        await self._conn.execute(
            "UPDATE video_jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE task_id=?",
            (*fields.values(), task_id),
        )
        await self._conn.commit()

    async def video_command(self, gid, action):
        if action not in ("pause", "resume", "cancel", "purge", "retry"):
            raise ValueError("invalid video action")
        await self._conn.execute(
            "UPDATE video_jobs SET command=? WHERE task_id=(SELECT id FROM tasks WHERE gid=? AND backend='video')",
            (action, gid),
        )
        await self._conn.commit()

    async def video_account(self, platform, owner_id):
        cur = await self._conn.execute(
            "SELECT * FROM video_accounts WHERE platform=? AND owner_id=?", (platform, owner_id)
        )
        return await cur.fetchone()

    async def video_put_account(self, platform, owner_id, identity, cookies):
        await self._conn.execute(
            """INSERT INTO video_accounts VALUES (?,?,?,?,0,?) ON CONFLICT(platform,owner_id)
               DO UPDATE SET identity=excluded.identity,cookies=excluded.cookies,enabled=0,updated=excluded.updated""",
            (platform, owner_id, identity, cookies, time.time()),
        )
        await self._conn.commit()

    async def video_remove_account(self, platform, owner_id):
        await self._conn.execute("DELETE FROM video_accounts WHERE platform=? AND owner_id=?", (platform, owner_id))
        await self._conn.execute(
            "UPDATE video_logins SET state='cancelled' WHERE platform=? AND owner_id=? AND state IN ('queued','waiting','scanned')",
            (platform, owner_id),
        )
        await self._conn.commit()

    async def video_share(self, platform, enabled):
        await self._conn.execute(
            "INSERT OR REPLACE INTO video_preferences VALUES (0,?,?)", (platform, "shared" if enabled else "anonymous")
        )
        await self._conn.execute(
            "UPDATE video_accounts SET enabled=? WHERE platform=? AND owner_id=0", (int(enabled), platform)
        )
        await self._conn.commit()

    async def video_preference(self, user_id, platform, mode=None):
        if mode is not None:
            if mode not in ("personal", "shared", "anonymous"):
                raise ValueError("invalid account mode")
            await self._conn.execute(
                "INSERT OR REPLACE INTO video_preferences VALUES (?,?,?)", (user_id, platform, mode)
            )
            await self._conn.commit()
        cur = await self._conn.execute(
            "SELECT mode FROM video_preferences WHERE user_id=? AND platform=?", (user_id, platform)
        )
        row = await cur.fetchone()
        return row["mode"] if row else "personal"

    async def video_new_login(self, platform, owner_id, requester_id, chat_id=None):
        now = time.time()
        await self._conn.execute(
            "UPDATE video_logins SET state='cancelled' WHERE platform=? AND owner_id=? AND state IN ('queued','waiting','scanned')",
            (platform, owner_id),
        )
        login_id = secrets.token_hex(12)
        await self._conn.execute(
            "INSERT INTO video_logins (id,platform,owner_id,requester_id,state,expires,chat_id) VALUES (?,?,?,?,'queued',?,?)",
            (login_id, platform, owner_id, requester_id, now + 300, chat_id),
        )
        await self._conn.commit()
        return login_id

    async def video_login(self, login_id):
        cur = await self._conn.execute("SELECT * FROM video_logins WHERE id=?", (login_id,))
        return await cur.fetchone()

    async def video_login_update(self, login_id, **fields):
        allowed = {"state", "qr_path", "identity", "message_id", "notified_state"}
        if not fields or not fields.keys() <= allowed:
            raise ValueError("invalid login fields")
        await self._conn.execute(
            "UPDATE video_logins SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?",
            (*fields.values(), login_id),
        )
        await self._conn.commit()

    async def video_login_rows(self):
        cur = await self._conn.execute(
            "SELECT * FROM video_logins WHERE expires>? OR notified_state IS NULL", (time.time() - 60,)
        )
        return await cur.fetchall()

    async def video_grant(self, login_id):
        token = secrets.token_urlsafe(32)
        await self._conn.execute(
            "INSERT INTO video_web_grants VALUES (?,?,?,0)", (token_hash(token), login_id, time.time() + 300)
        )
        await self._conn.commit()
        return token

    async def video_exchange(self, token):
        cur = await self._conn.execute(
            "UPDATE video_web_grants SET consumed=1 WHERE token_hash=? AND consumed=0 AND expires>? RETURNING login_id",
            (token_hash(token), time.time()),
        )
        row = await cur.fetchone()
        if not row:
            await self._conn.commit()
            return None
        session = secrets.token_urlsafe(32)
        await self._conn.execute(
            "INSERT INTO video_web_sessions VALUES (?,?,?)", (token_hash(session), row["login_id"], time.time() + 600)
        )
        await self._conn.commit()
        return session

    async def video_web_login(self, session):
        cur = await self._conn.execute(
            "SELECT login_id FROM video_web_sessions WHERE token_hash=? AND expires>?",
            (token_hash(session), time.time()),
        )
        row = await cur.fetchone()
        return await self.video_login(row["login_id"]) if row else None

    async def video_rebind_session(self, session, login_id):
        await self._conn.execute(
            "UPDATE video_web_sessions SET login_id=? WHERE token_hash=? AND expires>?",
            (login_id, token_hash(session), time.time()),
        )
        await self._conn.commit()

    async def video_set_size(self, task_id, size):
        await self._conn.execute("UPDATE tasks SET file_size=? WHERE id=?", (size, task_id))
        await self._conn.commit()

    async def video_prune(self):
        now = time.time()
        await self._conn.execute("DELETE FROM video_web_grants WHERE expires<?", (now - 600,))
        await self._conn.execute("DELETE FROM video_web_sessions WHERE expires<?", (now,))
        await self._conn.execute("DELETE FROM video_logins WHERE expires<?", (now - 86400,))
        await self._conn.commit()

    async def video_complete_login(self, login_id, identity, encrypted):
        # Single writer transaction: cancellation/removal on another process
        # either wins before this update, or runs after the account is stored.
        cur = await self._conn.execute(
            "UPDATE video_logins SET state='success',identity=? WHERE id=? AND state IN ('queued','waiting','scanned') AND expires>? RETURNING platform,owner_id",
            (identity, login_id, time.time()),
        )
        row = await cur.fetchone()
        if row:
            await self._conn.execute(
                """INSERT INTO video_accounts VALUES (?,?,?,?,0,?) ON CONFLICT(platform,owner_id)
                   DO UPDATE SET identity=excluded.identity,cookies=excluded.cookies,enabled=0,updated=excluded.updated""",
                (row["platform"], row["owner_id"], identity, encrypted, time.time()),
            )
        await self._conn.commit()
        return bool(row)
