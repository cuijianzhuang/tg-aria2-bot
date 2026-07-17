import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta

from bot.db.repo import TaskRepo


class RepoTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _create(self, gid="g1", **overrides):
        kwargs = dict(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref="ref", file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin",
        )
        kwargs.update(overrides)
        return await self.repo.create_task(**kwargs)


class TestTaskLifecycle(RepoTestCase):
    async def test_create_and_status_flow(self):
        await self._create()
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["finished_at"])

        await self.repo.update_status("g1", "ACTIVE")
        self.assertIsNone((await self.repo.get_by_gid("g1"))["finished_at"])

        await self.repo.update_status("g1", "COMPLETED", save_path="/downloads/f.bin")
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "COMPLETED")
        self.assertEqual(row["save_path"], "/downloads/f.bin")
        self.assertIsNotNone(row["finished_at"])

    async def test_retry_resets_terminal_fields(self):
        task_id = await self._create()
        await self.repo.update_status("g1", "FAILED", error="boom")
        await self.repo.retry_task(task_id, "g2", reply_message_id=42)
        row = await self.repo.get_by_id(task_id)
        self.assertEqual(row["gid"], "g2")
        self.assertEqual(row["status"], "PENDING")
        self.assertIsNone(row["error"])
        self.assertIsNone(row["finished_at"])
        self.assertEqual(row["reply_message_id"], 42)

    async def test_dedup_lookup_and_unfinished(self):
        await self._create("g1")
        await self.repo.update_status("g1", "COMPLETED")
        self.assertIsNotNone(await self.repo.get_completed_by_source("url", "ref"))
        self.assertIsNone(await self.repo.get_completed_by_source("url", "other"))
        await self._create("g2", source_ref="other")
        unfinished = await self.repo.get_unfinished()
        self.assertEqual([r["gid"] for r in unfinished], ["g2"])

    async def test_count_by_status(self):
        await self._create("g1")
        await self._create("g2", source_ref="x")
        await self.repo.update_status("g2", "FAILED")
        counts = await self.repo.count_by_status()
        self.assertEqual(counts["PENDING"], 1)
        self.assertEqual(counts["FAILED"], 1)


class TestAutoCleanup(RepoTestCase):
    async def _finished_days_ago(self, gid: str, days: int, *, source_ref: str):
        await self._create(gid, source_ref=source_ref)
        await self.repo.update_status(gid, "COMPLETED")
        finished_at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        await self.repo._conn.execute(
            "UPDATE tasks SET finished_at = ? WHERE gid = ?", (finished_at, gid)
        )
        await self.repo._conn.commit()

    async def test_deletes_only_older_than_cutoff(self):
        await self._finished_days_ago("old", 10, source_ref="a")
        await self._finished_days_ago("recent", 1, source_ref="b")
        cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
        deleted = await self.repo.delete_old_completed(cutoff)
        self.assertEqual(deleted, 1)
        self.assertIsNone(await self.repo.get_by_gid("old"))
        self.assertIsNotNone(await self.repo.get_by_gid("recent"))

    async def test_never_touches_non_completed(self):
        await self._create("active", source_ref="c")
        cutoff = (datetime.now(UTC) + timedelta(days=1)).isoformat()  # 未来，理论上会清光已完成的
        deleted = await self.repo.delete_old_completed(cutoff)
        self.assertEqual(deleted, 0)
        self.assertIsNotNone(await self.repo.get_by_gid("active"))


class TestPending(RepoTestCase):
    async def _pending(self):
        return await self.repo.create_pending(
            kind="url", user_id=1, chat_id=1, source_ref="r",
            file_name="f", file_size=1, payload="u",
        )

    async def test_pop_is_one_shot(self):
        token = await self._pending()
        self.assertIsNotNone(await self.repo.pop_pending(token))
        self.assertIsNone(await self.repo.pop_pending(token))

    async def test_restore_after_failed_start(self):
        token = await self._pending()
        pending = await self.repo.pop_pending(token)
        await self.repo.restore_pending(pending)
        again = await self.repo.pop_pending(token)
        self.assertIsNotNone(again)
        self.assertEqual(again.payload, "u")

    async def test_expired_pending_cleaned_up(self):
        token = await self._pending()
        await self.repo._conn.execute(
            "UPDATE pending_tasks SET created_at = created_at - 999999 WHERE token = ?", (token,)
        )
        await self.repo._conn.commit()
        self.assertIsNone(await self.repo.get_pending(token))


class TestAllowedUsers(RepoTestCase):
    async def test_add_check_remove(self):
        self.assertFalse(await self.repo.is_user_allowed(5))
        await self.repo.add_allowed_user(5, "note")
        self.assertTrue(await self.repo.is_user_allowed(5))
        await self.repo.remove_allowed_user(5)
        self.assertFalse(await self.repo.is_user_allowed(5))


class TestPendingBatch(RepoTestCase):
    async def _batch_pending(self, batch_id: str, n: int) -> list[str]:
        tokens = []
        for i in range(n):
            token = await self.repo.create_pending(
                kind="url", user_id=1, chat_id=1, source_ref=f"r{i}",
                file_name=f"f{i}", file_size=1, payload=f"u{i}", batch_id=batch_id,
            )
            tokens.append(token)
        return tokens

    async def test_get_pending_batch_returns_all_members_in_order(self):
        await self._batch_pending("b1", 3)
        pendings = await self.repo.get_pending_batch("b1")
        self.assertEqual([p.payload for p in pendings], ["u0", "u1", "u2"])

    async def test_batch_isolated_from_other_batches(self):
        await self._batch_pending("b1", 2)
        await self._batch_pending("b2", 1)
        self.assertEqual(len(await self.repo.get_pending_batch("b1")), 2)
        self.assertEqual(len(await self.repo.get_pending_batch("b2")), 1)

    async def test_delete_pending_batch_removes_only_that_batch(self):
        await self._batch_pending("b1", 2)
        await self._batch_pending("b2", 1)
        deleted = await self.repo.delete_pending_batch("b1")
        self.assertEqual(deleted, 2)
        self.assertEqual(await self.repo.get_pending_batch("b1"), [])
        self.assertEqual(len(await self.repo.get_pending_batch("b2")), 1)

    async def test_single_pending_has_no_batch_id(self):
        token = await self.repo.create_pending(
            kind="url", user_id=1, chat_id=1, source_ref="r", file_name="f",
            file_size=1, payload="u",
        )
        pending = await self.repo.get_pending(token)
        self.assertIsNone(pending.batch_id)


class TestSearchTasks(RepoTestCase):
    async def test_matches_file_name_case_insensitive(self):
        await self._create("g1", file_name="Movie.2024.mkv")
        await self._create("g2", source_ref="x", file_name="show.s01e01.mp4")
        results = await self.repo.search_tasks("movie")
        self.assertEqual([r["gid"] for r in results], ["g1"])

    async def test_matches_source_ref_too(self):
        await self._create("g1", file_name=None, source_ref="abc123hash")
        results = await self.repo.search_tasks("abc123")
        self.assertEqual([r["gid"] for r in results], ["g1"])

    async def test_no_match_returns_empty(self):
        await self._create("g1", file_name="movie.mkv")
        self.assertEqual(await self.repo.search_tasks("nonexistent"), [])

    async def test_orders_newest_first(self):
        await self._create("g1", file_name="dup.mkv", source_ref="a")
        await self._create("g2", file_name="dup.mkv", source_ref="b")
        results = await self.repo.search_tasks("dup")
        self.assertEqual([r["gid"] for r in results], ["g2", "g1"])


class TestPeriodStats(RepoTestCase):
    async def _created_days_ago(self, gid: str, days: int, *, status: str, file_size: int, source_ref: str):
        await self._create(gid, source_ref=source_ref, file_size=file_size)
        if status != "PENDING":
            await self.repo.update_status(gid, status)
        created_at = (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        await self.repo._conn.execute("UPDATE tasks SET created_at = ? WHERE gid = ?", (created_at, gid))
        await self.repo._conn.commit()

    async def test_counts_and_bytes_within_period(self):
        await self._created_days_ago("old", 10, status="COMPLETED", file_size=1000, source_ref="a")
        await self._created_days_ago("recent_ok", 1, status="COMPLETED", file_size=500, source_ref="b")
        await self._created_days_ago("recent_fail", 1, status="FAILED", file_size=0, source_ref="c")
        since = (datetime.now(UTC) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        stats = await self.repo.get_period_stats(since)
        self.assertEqual(stats["total"], 2)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["failed"], 1)
        self.assertEqual(stats["total_bytes"], 500)

    async def test_none_since_covers_everything(self):
        await self._created_days_ago("old", 100, status="COMPLETED", file_size=1, source_ref="a")
        stats = await self.repo.get_period_stats(None)
        self.assertEqual(stats["total"], 1)

    async def test_empty_db_returns_zeros_not_none(self):
        stats = await self.repo.get_period_stats(None)
        self.assertEqual(stats, {"total": 0, "completed": 0, "failed": 0, "cancelled": 0, "total_bytes": 0})


if __name__ == "__main__":
    unittest.main()
