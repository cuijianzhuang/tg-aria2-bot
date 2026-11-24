"""ParseHub backend: mapping, availability, probe cache, domains, DB, worker."""

import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core import quota
from bot.core.aria2_client import Download, File
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from bot.video import parsehub_backend
from bot.video.accounts import is_video_url, parsehub_platform_for
from bot.video.backends import MediaInfo, VideoResolver
from bot.video.parsehub_backend import ParseHubBackend, map_result
from bot.video.worker import VideoWorker


class VideoRef:
    def __init__(self, url, ext="mp4", width=0, height=0, duration=0):
        self.url, self.ext, self.width, self.height, self.duration = url, ext, width, height, duration


class ImageRef:
    def __init__(self, url, ext="jpg", width=0, height=0):
        self.url, self.ext, self.width, self.height = url, ext, width, height


class MusicRef:
    def __init__(self, url, ext="m4a", duration=0):
        self.url, self.ext, self.duration = url, ext, duration


class LivePhotoRef:
    def __init__(self, url, video_url=None):
        self.url, self.ext, self.width, self.height = url, "jpg", 100, 100
        self.video_url, self.video_ext, self.duration = video_url, "mp4", 3


def fake_result(type_, title="", content="", media=None):
    return SimpleNamespace(type=type_, title=title, content=content, media=media)


class TestParseHubMapping(unittest.TestCase):
    def test_video_result(self):
        info = map_result(fake_result("video", "标题", media=VideoRef("https://x/v.mp4", duration=9)))
        self.assertEqual(info.kind, "video")
        self.assertEqual(info.parser, "parsehub")
        self.assertEqual(info.qualities, ["best"])
        self.assertEqual(len(info.items), 1)
        self.assertEqual(info.items[0].kind, "video")
        self.assertEqual(info.items[0].duration, 9)

    def test_image_and_multi_and_music_and_rich_kinds(self):
        image = map_result(fake_result("image", media=[ImageRef("https://x/1.jpg"), ImageRef("https://x/2.jpg")]))
        self.assertEqual(image.kind, "image")
        self.assertEqual([i.kind for i in image.items], ["image", "image"])
        multi = map_result(fake_result("multimedia", media=[VideoRef("https://x/v.mp4"), ImageRef("https://x/p.jpg")]))
        self.assertEqual(multi.kind, "multi")
        music = map_result(fake_result("music", media=MusicRef("https://x/a.m4a")))
        self.assertEqual(music.kind, "music")
        rich = map_result(fake_result("richtext", "文章", content="正文内容", media=None))
        self.assertEqual(rich.kind, "rich")
        self.assertEqual(rich.content, "正文内容")
        self.assertEqual(rich.items, [])

    def test_title_falls_back_to_content_and_live_photo_keeps_video(self):
        info = map_result(
            fake_result("image", content="第一行\n第二行", media=LivePhotoRef("https://x/p.jpg", "https://x/p.mp4"))
        )
        self.assertEqual(info.title, "第一行")
        self.assertEqual([i.kind for i in info.items], ["image", "video"])

    def test_cookie_conversion(self):
        cookies = [{"name": "a", "value": "1"}, {"name": "b", "value": "2"}]
        self.assertEqual(parsehub_backend.cookies_to_dict(cookies), {"a": "1", "b": "2"})
        self.assertIsNone(parsehub_backend.cookies_to_dict([]))


class TestParseHubAvailability(unittest.IsolatedAsyncioTestCase):
    async def test_missing_package_disables_backend_with_reason(self):
        with patch("bot.video.parsehub_backend.importlib.util.find_spec", return_value=None):
            reason = parsehub_backend.unavailable_reason()
            self.assertIn("未安装", reason)
            with self.assertRaises(ValueError) as ctx:
                await ParseHubBackend().parse("https://weibo.com/x")
            self.assertIn("未安装", str(ctx.exception))

    async def test_explicit_probe_surfaces_unavailable_reason(self):
        resolver = VideoResolver()
        with patch.object(
            ParseHubBackend, "parse", AsyncMock(side_effect=ValueError("ParseHub 后端未安装，已自动禁用"))
        ):
            with self.assertRaises(ValueError) as ctx:
                await resolver.probe("https://example.com/v", parser="parsehub")
            self.assertIn("已自动禁用", str(ctx.exception))

    async def test_auto_probe_skips_unavailable_parsehub_silently(self):
        resolver = VideoResolver()
        cli_json = '{"title": "T", "formats": [{"height": 720, "vcodec": "avc", "filesize": 10}]}'
        with (
            patch.object(
                ParseHubBackend, "parse", AsyncMock(side_effect=ValueError("ParseHub 后端未安装，已自动禁用"))
            ),
            patch("bot.video.backends.capture", AsyncMock(return_value=cli_json)),
        ):
            info = await resolver.probe("https://youtube.com/watch?v=1", parser="auto")
        self.assertEqual(info.parser, "yt-dlp")


class TestProbeCache(unittest.IsolatedAsyncioTestCase):
    async def test_hit_expiry_and_account_mode_key(self):
        resolver = VideoResolver()
        calls = 0

        async def fake_uncached(url, **kwargs):
            nonlocal calls
            calls += 1
            return MediaInfo("T", "yt-dlp", ["best"])

        resolver._probe_uncached = fake_uncached
        await resolver.probe("https://example.com/v", account_mode="personal")
        await resolver.probe("https://example.com/v", account_mode="personal")
        self.assertEqual(calls, 1)
        await resolver.probe("https://example.com/v", account_mode="shared")
        self.assertEqual(calls, 2)
        key = ("https://example.com/v", "auto", "best", "personal")
        stored_at, info = resolver._cache[key]
        resolver._cache[key] = (stored_at - 301, info)
        await resolver.probe("https://example.com/v", account_mode="personal")
        self.assertEqual(calls, 3)


class TestParseHubDomains(unittest.TestCase):
    def test_parsehub_only_domains_are_recognised(self):
        cases = {
            "https://weibo.com/123/abc": "weibo",
            "https://www.zhihu.com/question/1/answer/2": "zhihu",
            "https://tieba.baidu.com/p/123": "tieba",
            "https://mp.weixin.qq.com/s/xyz": "weixin",
            "https://www.kuaishou.com/short-video/abc": "kuaishou",
            "https://www.toutiao.com/article/1": "toutiao",
            "https://www.douban.com/group/topic/1": "douban",
            "https://www.coolapk.com/feed/1": "coolapk",
            "https://www.threads.com/@u/post/abc": "threads",
        }
        for url, platform in cases.items():
            self.assertEqual(parsehub_platform_for(url), platform, url)
            self.assertTrue(is_video_url(url), url)
        self.assertIsNone(parsehub_platform_for("https://example.com/v"))
        self.assertFalse(is_video_url("https://example.com/file.zip"))


class TestVideoJobKindStorage(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.directory.name) / "tasks.db"))
        await self.repo.connect()

    async def asyncTearDown(self):
        await self.repo.close()
        self.directory.cleanup()

    async def test_kind_content_columns_and_complete_files(self):
        columns = await (await self.repo._conn.execute("PRAGMA table_info(video_jobs)")).fetchall()
        self.assertTrue({"kind", "files", "content"} <= {c["name"] for c in columns})
        token = await self.repo.video_save_preview(
            user_id=1,
            chat_id=1,
            url="https://weibo.com/1/2",
            parser="parsehub",
            quality="best",
            account_mode="anonymous",
            info={"title": "图文", "qualities": ["best"], "kind": "image", "content": "正文", "items": []},
        )
        row = await self.repo.video_create_task(token, 1, 1)
        job = await self.repo.video_job(row["id"])
        self.assertEqual(job["kind"], "image")
        self.assertEqual(job["content"], "正文")
        await self.repo.video_complete_files(row["id"], ["/d/1.jpg", "/d/2.jpg"], 30, "image")
        job = await self.repo.video_job(row["id"])
        self.assertEqual(job["files"], '["/d/1.jpg", "/d/2.jpg"]')
        latest = await self.repo.get_by_gid(row["gid"])
        self.assertEqual(latest["status"], "COMPLETED")
        self.assertEqual(latest["save_path"], "/d/1.jpg")
        self.assertEqual(latest["file_size"], 30)


def make_image(path, size=(64, 48)):
    from PIL import Image

    Image.new("RGB", size, (200, 30, 30)).save(path, "JPEG")


class TestParseHubWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.video.worker.storage.has_enough_space", return_value=True))
        self.stack.enter_context(patch.object(settings, "db_path", str(root / "tasks.db")))
        self.stack.enter_context(patch.object(settings, "download_dir", str(root / "downloads")))
        self.stack.enter_context(patch.object(settings, "user_max_task_size_gb", 0))
        (root / "downloads").mkdir()
        self.repo = TaskRepo(settings.db_path)
        await self.repo.connect()
        self.manager = SimpleNamespace(
            pending_usage=lambda user: 0,
            usage_today=lambda user: self.repo.get_usage(user, quota.today()),
            complete_local_video=AsyncMock(),
            complete_local_media=AsyncMock(),
        )
        self.worker = VideoWorker(AsyncMock(), self.repo, self.manager)

    async def asyncTearDown(self):
        self.stack.close()
        await self.repo.close()
        self.directory.cleanup()

    async def make_row(self, kind, items):
        token = await self.repo.video_save_preview(
            user_id=1,
            chat_id=1,
            url="https://weibo.com/1/2",
            parser="parsehub",
            quality="best",
            account_mode="anonymous",
            info={"title": "图文标题", "qualities": ["best"], "kind": kind, "content": "正文", "items": items},
        )
        task = await self.repo.video_create_task(token, 1, 1)
        return dict(next(r for r in await self.repo.video_jobs() if r["id"] == task["id"]))

    async def test_image_job_downloads_verifies_and_uses_media_completion(self):
        row = await self.make_row("image", [{"url": "https://x/1.jpg", "kind": "image", "ext": "jpg"}])
        info = MediaInfo(
            "图文标题",
            "parsehub",
            ["best"],
            None,
            kind="image",
            content="正文",
            items=[],
        )
        self.worker.resolver.probe = AsyncMock(return_value=info)

        async def fake_download(url, directory, **kwargs):
            callback = kwargs.get("callback")
            sub = Path(directory) / "post"
            sub.mkdir(parents=True, exist_ok=True)
            paths = [sub / "1.jpg", sub / "2.jpg"]
            for path in paths:
                make_image(path)
            if callback:
                await callback(10, 20, "bytes")
                await callback(2, 2, "count")
            return paths

        self.worker.resolver.download_parsehub = fake_download
        await self.worker._execute(row)
        latest = await self.repo.get_by_gid(row["gid"])
        self.assertEqual(latest["status"], "COMPLETED", latest["error"])
        job = await self.repo.video_job(row["id"])
        self.assertEqual(job["kind"], "image")
        self.assertEqual(len(json.loads(job["files"])), 2)
        self.manager.complete_local_media.assert_awaited_once()
        caption = self.manager.complete_local_media.await_args.args[2]
        self.assertIn("正文", caption)
        self.manager.complete_local_video.assert_not_awaited()

    async def test_rich_job_without_media_writes_article(self):
        row = await self.make_row("rich", [])
        info = MediaInfo("文章标题", "parsehub", ["best"], None, kind="rich", content="全文正文", items=[])
        self.worker.resolver.probe = AsyncMock(return_value=info)
        self.worker.resolver.download_parsehub = AsyncMock(return_value=[])
        await self.worker._execute(row)
        latest = await self.repo.get_by_gid(row["gid"])
        self.assertEqual(latest["status"], "COMPLETED", latest["error"])
        article = Path(latest["save_path"])
        self.assertEqual(article.name, "article.txt")
        self.assertIn("全文正文", article.read_text(encoding="utf-8"))
        self.manager.complete_local_media.assert_awaited_once()


class TestMediaGroupDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.repo = TaskRepo(str(root / "tasks.db"))
        await self.repo.connect()
        token = await self.repo.video_save_preview(
            user_id=1,
            chat_id=99,
            url="https://weibo.com/1/2",
            parser="parsehub",
            quality="best",
            account_mode="anonymous",
            info={"title": "图文", "qualities": ["best"], "kind": "image"},
        )
        self.row = await self.repo.video_create_task(token, 1, 1)
        self.bot = AsyncMock()
        self.manager = TaskManager(self.bot, None, self.repo)
        self.manager._notify = AsyncMock()

    async def asyncTearDown(self):
        await self.repo.close()
        self.directory.cleanup()

    async def test_media_group_sent_with_caption(self):
        root = Path(self.directory.name)
        paths = [root / "1.jpg", root / "2.jpg"]
        for path in paths:
            make_image(path)
        download = Download(
            self.row["gid"],
            "complete",
            10,
            10,
            0,
            0,
            0,
            None,
            root,
            [File(i + 1, p, 5, 5, True) for i, p in enumerate(paths)],
        )
        await self.manager.complete_local_media(self.row, download, "标题\n\n正文")
        await asyncio.sleep(0.1)
        self.bot.send_media_group.assert_awaited_once()
        media = self.bot.send_media_group.await_args.kwargs["media"]
        self.assertEqual(len(media), 2)
        self.assertEqual(media[0].caption, "标题\n\n正文")
        latest = await self.repo.get_by_gid(self.row["gid"])
        self.assertEqual(latest["status"], "COMPLETED")

    async def test_rich_text_sent_as_message(self):
        root = Path(self.directory.name)
        article = root / "article.txt"
        article.write_text("标题\n\n全文", encoding="utf-8")
        download = Download(self.row["gid"], "complete", 5, 5, 0, 0, 0, None, root, [File(1, article, 5, 5, True)])
        await self.manager.complete_local_media(self.row, download, "标题\n\n全文")
        await asyncio.sleep(0.1)
        self.bot.send_message.assert_awaited()
        self.assertIn("全文", self.bot.send_message.await_args.kwargs["text"])


if __name__ == "__main__":
    unittest.main()
