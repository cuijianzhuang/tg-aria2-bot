"""RSS 订阅：解析、过滤、基线（不下载历史）、去重、额度、/rss 命令。"""
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core import rss
from bot.core.rss import FeedError
from bot.db.repo import TaskRepo
from bot.handlers import rss as rss_handler
from tests.fakes import FakeNodePool

RSS = b"""<?xml version="1.0"?>
<rss version="2.0" xmlns:torrent="http://xmlns.ezrss.it/0.1/"><channel><title>Anime Feed</title>
<item><title>Show A 01 1080p</title><guid>a1</guid><enclosure url="https://x.test/a1.torrent" type="application/x-bittorrent"/></item>
<item><title>Show A 01 720p</title><guid>a1b</guid><link>https://x.test/a1b.torrent</link></item>
<item><title>Show B 05 1080p</title><guid>b5</guid><torrent:magnetURI>magnet:?xt=urn:btih:abcdef1234567890abcdef1234567890abcdef12</torrent:magnetURI></item>
<item><title>No source here</title><guid>n1</guid></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom Feed</title>
<entry><title>Entry 1</title><id>tag:1</id><link rel="alternate" href="https://x.test/e1.torrent"/></entry>
<entry><title>Entry 2</title><id>tag:2</id><link rel="enclosure" href="magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567"/></entry>
</feed>"""


class TestParse(unittest.TestCase):
    def test_rss_items_and_source_priority(self):
        title, items = rss.parse_feed(RSS)
        self.assertEqual(title, "Anime Feed")
        by = {i.guid: i for i in items}
        self.assertEqual(by["a1"].source, "https://x.test/a1.torrent")          # enclosure
        self.assertEqual(by["a1b"].source, "https://x.test/a1b.torrent")        # link
        self.assertTrue(by["b5"].source.startswith("magnet:"))                  # 扩展标签
        self.assertIsNone(by["n1"].source)

    def test_atom(self):
        title, items = rss.parse_feed(ATOM)
        self.assertEqual(title, "Atom Feed")
        self.assertEqual(items[0].source, "https://x.test/e1.torrent")
        self.assertTrue(items[1].source.startswith("magnet:"))

    def test_entity_bombs_are_refused(self):
        bomb = b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;">]><rss><channel><title>&b;</title></channel></rss>'
        with self.assertRaises(FeedError):
            rss.parse_feed(bomb)

    def test_garbage(self):
        with self.assertRaises(FeedError):
            rss.parse_feed(b"<html>not a feed")

    def test_dangerous_schemes_are_not_sources(self):
        data = b"<rss><channel><item><title>x</title><guid>g</guid><link>file:///etc/passwd</link>" \
               b"<enclosure url='ftp://a/b'/></item></channel></rss>"
        _, items = rss.parse_feed(data)
        self.assertIsNone(items[0].source)


class TestMatches(unittest.TestCase):
    def test_all_keywords_required_case_insensitive(self):
        self.assertTrue(rss.matches("Show A 01 1080P", "show 1080p", ""))
        self.assertFalse(rss.matches("Show A 01 720p", "show 1080p", ""))

    def test_exclude(self):
        self.assertFalse(rss.matches("Show A 720p", "", "720p"))
        self.assertTrue(rss.matches("Show A 1080p", "", "720p"))

    def test_empty_filter_matches_everything(self):
        self.assertTrue(rss.matches("anything", "", ""))

    def test_commas_and_chinese_commas(self):
        self.assertTrue(rss.matches("简体 1080p", "简体，1080p", ""))


class TestValidateUrl(unittest.TestCase):
    def test_ok(self):
        self.assertEqual(rss.validate_url(" https://example.com/feed.xml "), "https://example.com/feed.xml")

    def test_rejected(self):
        for bad in ("ftp://x/y", "file:///etc/passwd", "javascript:1", "http://localhost/f", "http://127.0.0.1/f",
                    "http://10.0.0.5/f", "http://192.168.1.1/f", "http://169.254.169.254/latest", "http://[::1]/f",
                    "http://printer.local/f", "not a url"):
            with self.assertRaises(FeedError, msg=bad):
                rss.validate_url(bad)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)
        return SimpleNamespace(message_id=1000 + len(self.sent))


class PollBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.addAsyncCleanup(self.repo.close)
        self.nodes = FakeNodePool(download_dir=os.path.join(self._dir.name, "dl"))
        os.makedirs(self.nodes.get_node("default").download_dir)
        self.bot = FakeBot()
        self._orig = (settings.admin_user_ids, settings.allowed_user_ids, settings.user_max_active_tasks,
                      settings.user_max_task_size_gb, settings.user_daily_quota_gb)
        settings.admin_user_ids = "1"
        settings.allowed_user_ids = "1,5"
        settings.user_max_active_tasks = 0
        settings.user_daily_quota_gb = 0
        self.addCleanup(lambda: (setattr(settings, "admin_user_ids", self._orig[0]),
                                 setattr(settings, "allowed_user_ids", self._orig[1]),
                                 setattr(settings, "user_max_active_tasks", self._orig[2]),
                                 setattr(settings, "user_max_task_size_gb", self._orig[3]),
                                 setattr(settings, "user_daily_quota_gb", self._orig[4])))
        self.body = RSS

        async def fake_fetch(url):
            if isinstance(self.body, Exception):
                raise self.body
            return self.body
        self.added = []

        async def fake_add(nodes, node_name, kind, payload, file_name):
            self.added.append((kind, payload, file_name))
            return f"gid{len(self.added)}"
        for p in (patch.object(rss, "fetch", fake_fetch), patch("bot.handlers.callbacks._add_source", fake_add)):
            p.start()
            self.addCleanup(p.stop)

    async def _feed(self, keyword="", exclude="", user_id=5):
        fid = await self.repo.add_feed(user_id=user_id, chat_id=500, url="https://x.test/f", keyword=keyword,
                                       exclude=exclude, node="default")
        return fid

    async def _poll(self, fid, tm=None):
        return await rss.poll_feed(await self.repo.get_feed(fid), bot=self.bot, repo=self.repo, nodes=self.nodes,
                                   task_manager=tm)

    async def _tasks(self):
        cur = await self.repo._conn.execute("SELECT * FROM tasks ORDER BY id")
        return await cur.fetchall()


class TestPoll(PollBase):
    async def test_first_poll_is_a_baseline_and_downloads_nothing(self):
        fid = await self._feed()
        self.assertEqual(await self._poll(fid), 0)
        self.assertEqual(await self._tasks(), [])
        self.assertEqual(len(await self.repo.seen_guids(fid)), 4)
        self.assertEqual((await self.repo.get_feed(fid))["title"], "Anime Feed")

    async def test_new_items_after_baseline_are_downloaded_and_announced(self):
        fid = await self._feed()
        self.body = b"<rss><channel><title>T</title></channel></rss>"
        await self._poll(fid)                                    # 空源建基线
        self.body = RSS
        added = await self._poll(fid)
        self.assertEqual(added, 3)                               # n1 没有下载源，被跳过
        tasks = await self._tasks()
        self.assertEqual({t["user_id"] for t in tasks}, {5})
        self.assertEqual(len(self.bot.sent), 3)
        self.assertIsNotNone(tasks[0]["reply_message_id"])      # 之后进度才能编辑回这条消息
        self.assertEqual(sorted(k for k, _, _ in self.added), ["magnet", "url", "url"])

    async def _baseline_empty_then(self, fid, body):
        self.body = b"<rss><channel><title>T</title></channel></rss>"
        await self._poll(fid)
        self.body = body

    async def test_nothing_is_added_twice(self):
        fid = await self._feed()
        await self._baseline_empty_then(fid, RSS)
        await self._poll(fid)
        self.assertEqual(await self._poll(fid), 0)
        self.assertEqual(len(await self._tasks()), 3)

    async def test_keyword_and_exclude_filters(self):
        fid = await self._feed(keyword="1080p", exclude="05")
        await self._baseline_empty_then(fid, RSS)
        self.assertEqual(await self._poll(fid), 1)
        self.assertEqual((await self._tasks())[0]["payload"], "https://x.test/a1.torrent")

    async def test_filtered_out_items_are_not_retried_later(self):
        fid = await self._feed(keyword="nomatch")
        await self._baseline_empty_then(fid, RSS)
        await self._poll(fid)
        self.assertEqual(len(await self.repo.seen_guids(fid)), 4)

    async def test_quota_refusal_defers_items_to_the_next_round(self):
        settings.user_max_active_tasks = 1
        fid = await self._feed()
        await self._baseline_empty_then(fid, RSS)
        self.assertEqual(await self._poll(fid), 1)               # 名额只有 1 个
        self.assertEqual(len(await self._tasks()), 1)
        await self.repo._conn.execute("UPDATE tasks SET status = 'COMPLETED'")
        await self.repo._conn.commit()
        self.assertEqual(await self._poll(fid), 1)               # 腾出名额后接着来

    async def test_admin_is_not_limited_by_quota(self):
        settings.user_max_active_tasks = 1
        fid = await self._feed(user_id=1)
        await self._baseline_empty_then(fid, RSS)
        self.assertEqual(await self._poll(fid), 3)

    async def test_already_downloaded_items_are_skipped(self):
        fid = await self._feed()
        await self._baseline_empty_then(fid, RSS)
        from bot.core import storage
        ref = storage.url_hash("https://x.test/a1.torrent")
        await self.repo.create_task(gid="old", user_id=9, chat_id=9, reply_message_id=None, source_type="url",
                                    source_ref=ref, file_name="old", file_size=1)
        await self.repo.update_status("old", "COMPLETED", save_path="/x")
        self.assertEqual(await self._poll(fid), 2)

    async def test_fetch_error_is_recorded_and_does_not_break_the_baseline(self):
        fid = await self._feed()
        self.body = FeedError("订阅源返回 HTTP 500")
        self.assertEqual(await self._poll(fid), 0)
        feed = await self.repo.get_feed(fid)
        self.assertIn("500", feed["last_error"])
        self.assertIsNone(feed["last_checked"])                 # 还没成功过，下次成功仍然是基线
        self.body = RSS
        self.assertEqual(await self._poll(fid), 0)
        self.assertEqual(await self._tasks(), [])               # 没把历史条目全下载下来
        self.assertIsNone((await self.repo.get_feed(fid))["last_error"])

    async def test_add_failure_is_retried_next_round(self):
        fid = await self._feed(keyword="show a 01 1080p")
        await self._baseline_empty_then(fid, RSS)
        good = self.added
        with patch("bot.handlers.callbacks._add_source", AsyncMock(side_effect=RuntimeError("aria2 down"))):
            self.assertEqual(await self._poll(fid), 0)
        self.assertEqual(good, [])
        self.assertEqual(await self._poll(fid), 1)    # 失败的条目没记账，恢复后补上


    async def test_per_round_cap(self):
        items = "".join(f"<item><title>T{i}</title><guid>g{i}</guid><link>https://x.test/{i}.bin</link></item>" for i in range(30))
        fid = await self._feed()
        await self._baseline_empty_then(fid, f"<rss><channel><title>T</title>{items}</channel></rss>".encode())
        self.assertEqual(await self._poll(fid), rss.MAX_NEW_PER_POLL)
        self.assertEqual(await self._poll(fid), 10)


class FakeMsg:
    def __init__(self, user_id=5):
        self.from_user = SimpleNamespace(id=user_id)
        self.chat = SimpleNamespace(id=500)
        self.bot = FakeBot()
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)


class TestCommand(PollBase):
    async def _cmd(self, args, user_id=5):
        msg = FakeMsg(user_id)
        await rss_handler.cmd_rss(msg, SimpleNamespace(args=args), self.repo, self.nodes)
        return msg.replies[0]

    async def test_add_subscribes_and_baselines(self):
        reply = await self._cmd("add https://x.test/f 1080p -720p")
        self.assertIn("已订阅", reply)
        self.assertIn("Anime Feed", reply)
        feed = (await self.repo.list_feeds())[0]
        self.assertEqual((feed["keyword"], feed["exclude"], feed["user_id"]), ("1080p", "720p", 5))
        self.assertEqual(await self._tasks(), [])

    async def test_bad_feed_is_not_kept(self):
        self.body = b"<html>"
        reply = await self._cmd("add https://x.test/f")
        self.assertIn("订阅失败", reply)
        self.assertEqual(await self.repo.list_feeds(), [])

    async def test_internal_address_is_refused(self):
        self.assertIn("内网", await self._cmd("add http://127.0.0.1:6800/jsonrpc"))
        self.assertEqual(await self.repo.list_feeds(), [])

    async def test_duplicate(self):
        await self._cmd("add https://x.test/f")
        self.assertIn("已经订阅过", await self._cmd("add https://x.test/f"))

    async def test_per_user_limit_but_not_for_admin(self):
        settings.rss_max_feeds_per_user = 1
        self.addCleanup(lambda: setattr(settings, "rss_max_feeds_per_user", 5))
        await self._cmd("add https://x.test/1")
        self.assertIn("最多订阅", await self._cmd("add https://x.test/2"))
        self.assertIn("已订阅", await self._cmd("add https://x.test/1", user_id=1))
        self.assertIn("已订阅", await self._cmd("add https://x.test/2", user_id=1))

    async def test_list_shows_own_feeds_only_unless_admin(self):
        await self._cmd("add https://x.test/mine")
        await self._cmd("add https://x.test/theirs", user_id=7)
        self.assertEqual(len(await self.repo.list_feeds(user_id=5)), 1)
        text, _ = await rss_handler._list_view(self.repo, 5)
        self.assertNotIn("订阅人", text)
        text, _ = await rss_handler._list_view(self.repo, 1)
        self.assertIn("订阅人", text)

    async def test_usage_text_for_bad_subcommand(self):
        self.assertIn("/rss add", await self._cmd("subscribe foo"))


class FakeQuery:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(edit_text=AsyncMock())
        self.answers = []

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class TestButtons(PollBase):
    async def test_owner_can_pause_resume_delete(self):
        fid = await self._feed()
        q = FakeQuery(f"rss:toggle:{fid}", 5)
        await rss_handler.rss_action(q, self.repo)
        self.assertEqual((await self.repo.get_feed(fid))["enabled"], 0)
        await rss_handler.rss_action(FakeQuery(f"rss:toggle:{fid}", 5), self.repo)
        self.assertEqual((await self.repo.get_feed(fid))["enabled"], 1)
        await rss_handler.rss_action(FakeQuery(f"rss:del:{fid}", 5), self.repo)
        self.assertIsNone(await self.repo.get_feed(fid))

    async def test_others_cannot_but_admin_can(self):
        fid = await self._feed()
        q = FakeQuery(f"rss:del:{fid}", 7)
        await rss_handler.rss_action(q, self.repo)
        self.assertIsNotNone(await self.repo.get_feed(fid))
        self.assertTrue(q.answers[-1][1])
        await rss_handler.rss_action(FakeQuery(f"rss:del:{fid}", 1), self.repo)
        self.assertIsNone(await self.repo.get_feed(fid))

    async def test_deleting_clears_seen_records(self):
        fid = await self._feed()
        await self.repo.mark_seen(fid, ["a", "b"])
        await self.repo.delete_feed(fid)
        self.assertEqual(await self.repo.seen_guids(fid), set())

    async def test_disabled_feeds_are_not_polled_by_the_loop_query(self):
        fid = await self._feed()
        await self.repo.set_feed_enabled(fid, False)
        self.assertEqual(await self.repo.list_feeds(enabled_only=True), [])


if __name__ == "__main__":
    unittest.main()
