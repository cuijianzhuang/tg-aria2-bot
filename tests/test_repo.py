import os
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
