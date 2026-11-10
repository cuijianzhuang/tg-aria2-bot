import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core.metalink import MAX_METALINK_BYTES, split_metalink
from bot.db.repo import TaskRepo
from bot.handlers.links import handle_metalink
from tests.fakes import FakeNodePool


class TestMetalink(unittest.TestCase):
    def test_v3_and_v4_split_preserves_metadata_and_resources(self):
        for ns, wrapped in (("urn:ietf:params:xml:ns:metalink", False),
                            ("http://www.metalinker.org/", True)):
            files = ('<file name="a.iso"><url>ftp://example.com/a.iso</url></file>'
                     '<file name="b.bin"><url>sftp://example.com/b.bin</url></file>')
            body = f"<files>{files}</files>" if wrapped else files
            content = f'<metalink xmlns="{ns}"><identity>test</identity>{body}</metalink>'.encode()
            entries = split_metalink(content)
            self.assertEqual([name for name, _ in entries], ["a.iso", "b.bin"])
            for name, data in entries:
                root = ET.fromstring(data)
                selected = list(root.iter(f"{{{ns}}}file"))
                self.assertEqual(len(selected), 1)
                self.assertEqual(selected[0].get("name"), name)
                self.assertEqual(root.find(f"{{{ns}}}identity").text, "test")
                self.assertIsNotNone(selected[0].find(f"{{{ns}}}url"))

    def test_rejects_invalid_unsafe_and_oversized_manifests(self):
        for data in (b"bad xml", b"<metalink/>",
                     b'<metalink xmlns="urn:ietf:params:xml:ns:metalink"/>',
                     b'<!DOCTYPE metalink><metalink/>',
                     '<!DOCTYPE metalink><metalink/>'.encode("utf-16"),
                     b"x" * (MAX_METALINK_BYTES + 1)):
            with self.subTest(data=data[:30]), self.assertRaises(ValueError):
                split_metalink(data)

    def test_rejects_too_many_files(self):
        files = '<file name="a"/>' * 21
        with self.assertRaises(ValueError):
            split_metalink(f'<metalink xmlns="urn:ietf:params:xml:ns:metalink">{files}</metalink>'.encode())


class TestMetalinkUpload(unittest.IsolatedAsyncioTestCase):
    async def test_upload_creates_independent_persistent_pending_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = str(Path(directory) / "tasks.db")
            manifest = Path(directory) / "source.meta4"
            manifest.write_text(
                '<metalink xmlns="urn:ietf:params:xml:ns:metalink">'
                '<file name="a.iso"><url>https://example.com/a.iso</url></file>'
                '<file name="b.bin"><url>ftp://example.com/b.bin</url></file></metalink>'
            )
            repo = TaskRepo(db_path)
            await repo.connect()
            try:
                message = SimpleNamespace(
                    document=SimpleNamespace(file_size=manifest.stat().st_size,
                                             file_id="file", file_unique_id="unique"),
                    from_user=SimpleNamespace(id=1), chat=SimpleNamespace(id=1),
                    bot=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path=str(manifest)))),
                    reply=AsyncMock(),
                )
                with patch.object(settings, "db_path", db_path):
                    await handle_metalink(message, repo, FakeNodePool())
                keyboard = message.reply.await_args.kwargs["reply_markup"]
                callback = keyboard.inline_keyboard[0][0].callback_data
                self.assertTrue(callback.startswith("pending:startall:"))
                entries = await repo.get_pending_batch(callback.split(":")[-1])
                self.assertEqual(len(entries), 2)
                for entry in entries:
                    self.assertEqual(entry.kind, "metalink")
                    self.assertEqual(entry.node, "default")
                    self.assertNotEqual(entry.payload, str(manifest))
                    selected = split_metalink(Path(entry.payload).read_bytes())
                    self.assertEqual(len(selected), 1)
                    self.assertEqual(selected[0][0], entry.file_name)
            finally:
                await repo.close()
