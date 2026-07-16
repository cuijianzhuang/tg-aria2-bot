"""Pure-function rendering tests. Run on a machine with a valid .env:

    .venv/bin/python -m unittest discover tests -v
"""
import unittest

from bot.core.cards import _eta, _fmt_limit, _fmt_size, render_home, render_pending_card, render_task_card
from bot.core.keyboards import list_tab_row, task_keyboard, text_progress_bar


class FakeDownload:
    progress = 42.5
    download_speed = 1024 * 1024
    total_length = 200 * 1024 * 1024
    completed_length = 85 * 1024 * 1024
    connections = 8
    name = "file.bin"
    dir = "/downloads"

    def completed_length_string(self):
        return "85.0 MiB"

    def total_length_string(self):
        return "200.0 MiB"

    def download_speed_string(self):
        return "1.0 MiB/s"

    def upload_speed_string(self):
        return "0 B/s"


def fake_row(**overrides):
    row = {
        "id": 7,
        "gid": "abc123",
        "status": "ACTIVE",
        "file_name": "movie.mkv",
        "file_size": 200 * 1024 * 1024,
        "source_ref": "ref",
        "save_path": "/downloads/video",
        "error": None,
        "gofile_link": None,
        "payload": "https://example.com/movie.mkv",
        "source_type": "url",
    }
    row.update(overrides)
    return row


class TestProgressBar(unittest.TestCase):
    def test_bounds(self):
        self.assertEqual(text_progress_bar(0), "░" * 12)
        self.assertEqual(text_progress_bar(100), "█" * 12)
        self.assertEqual(text_progress_bar(150), "█" * 12)  # clamps
        self.assertEqual(text_progress_bar(-5), "░" * 12)


class TestFormatters(unittest.TestCase):
    def test_fmt_size(self):
        self.assertEqual(_fmt_size(None), "未知")
        self.assertEqual(_fmt_size(512), "512 B")
        self.assertEqual(_fmt_size(2 * 1024 * 1024), "2.0 MiB")

    def test_eta(self):
        self.assertEqual(_eta(FakeDownload()), "约 1分55秒")

    def test_eta_no_speed(self):
        d = FakeDownload()
        d.download_speed = 0
        self.assertEqual(_eta(d), "未知")

    def test_fmt_limit(self):
        self.assertEqual(_fmt_limit("0"), "不限速")
        self.assertEqual(_fmt_limit("2097152"), "2.0 MiB/s")
        self.assertEqual(_fmt_limit("garbage"), "garbage")


class TestCards(unittest.TestCase):
    def test_active_card_has_speed_line(self):
        text = render_task_card(fake_row(), FakeDownload(), status="ACTIVE")
        self.assertIn("⚡", text)
        self.assertIn("movie.mkv", text)

    def test_completed_card_hides_speed_line(self):
        text = render_task_card(fake_row(status="COMPLETED"), status="COMPLETED")
        self.assertNotIn("⚡", text)

    def test_card_escapes_html_in_name(self):
        text = render_task_card(fake_row(file_name="<b>x&y</b>.zip"), status="PENDING")
        self.assertIn("&lt;b&gt;x&amp;y&lt;/b&gt;.zip", text)

    def test_pending_card_escapes(self):
        text = render_pending_card("url", "<script>.bin")
        self.assertIn("&lt;script&gt;", text)

    def test_home_renders_without_stats(self):
        text = render_home({"ACTIVE": 2})
        self.assertIn("下载中 2", text)


class TestKeyboards(unittest.TestCase):
    def test_failed_keyboard_uses_retry(self):
        kb = task_keyboard("g1", "FAILED")
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("task:retry:g1", callbacks)
        self.assertNotIn("task:resume:g1", callbacks)

    def test_unknown_status_returns_none(self):
        self.assertIsNone(task_keyboard("g1", "NONSENSE"))

    def test_open_from_list_gets_back_button(self):
        kb = task_keyboard("g1", "ACTIVE", with_back=True)
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("list:ALL:0", callbacks)

    def test_tab_row_marks_selected(self):
        row = list_tab_row("ACTIVE", {"ACTIVE": 2, "COMPLETED": 5})
        labels = {b.callback_data: b.text for b in row}
        self.assertTrue(labels["list:ACTIVE:0"].startswith("·"))
        self.assertFalse(labels["list:COMPLETED:0"].startswith("·"))
        self.assertIn("7", labels["list:ALL:0"])  # ALL = sum of counts


if __name__ == "__main__":
    unittest.main()
