"""Pure-function rendering tests. Run on a machine with a valid .env:

    .venv/bin/python -m unittest discover tests -v
"""
import unittest

from bot.config import settings
from bot.core.cards import (
    _eta,
    _fmt_limit,
    _fmt_max_file_size,
    _fmt_size,
    render_batch_pending,
    render_cleanup_chooser,
    render_dir_chooser,
    render_file_selection,
    render_home,
    render_maxsize_chooser,
    render_pending_card,
    render_settings,
    render_stats,
    render_task_card,
    render_task_limit_chooser,
)
from bot.core.keyboards import (
    LIMIT_PRESETS,
    batch_pending_keyboard,
    cleanup_chooser_keyboard,
    concurrent_chooser_keyboard,
    dir_chooser_keyboard,
    file_selection_keyboard,
    list_tab_row,
    maxsize_chooser_keyboard,
    settings_keyboard,
    stats_period_keyboard,
    task_keyboard,
    task_limit_chooser_keyboard,
    text_progress_bar,
)


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


class FakeFile:
    def __init__(self, index, name, length, selected, is_metadata=False):
        self.index = index
        self.path = __import__("pathlib").Path(name)
        self._length = length
        self.selected = selected
        self.is_metadata = is_metadata

    def length_string(self):
        return f"{self._length} B"


class FakeMultiFileDownload:
    def __init__(self, files):
        self.files = files


class TestFileSelection(unittest.TestCase):
    def _download(self):
        return FakeMultiFileDownload([
            FakeFile(1, "movie.mkv", 100, True),
            FakeFile(2, "sample.mkv", 10, False),
            FakeFile(3, "readme.txt", 1, True),
        ])

    def test_render_counts_selected(self):
        text = render_file_selection(self._download())
        self.assertIn("已选 2/3", text)

    def test_keyboard_shows_checkbox_state(self):
        kb = file_selection_keyboard("g1", self._download())
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("☑️") and "movie.mkv" in l for l in labels))
        self.assertTrue(any(l.startswith("⬜") and "sample.mkv" in l for l in labels))

    def test_keyboard_excludes_metadata_files(self):
        download = FakeMultiFileDownload([
            FakeFile(1, "movie.mkv", 100, True),
            FakeFile(2, "[METADATA]torrent", 1, True, is_metadata=True),
        ])
        kb = file_selection_keyboard("g1", download)
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertNotIn("filesel:g1:2", callbacks)


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

    def test_settings_keyboard_includes_admin_entries(self):
        kb = settings_keyboard()
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        for expected in ("settings:limit", "settings:concurrent", "settings:notify",
                         "settings:maxsize", "settings:dir", "settings:cleanup", "settings:sendtg",
                         "admin:users", "admin:gofile", "admin:rclone", "admin:restart"):
            self.assertIn(expected, callbacks)

    def test_completed_keyboard_has_send_to_tg(self):
        kb = task_keyboard("g1", "COMPLETED")
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("task:sendtg:g1", callbacks)

    def test_task_limit_chooser_marks_current_and_links_gid(self):
        kb = task_limit_chooser_keyboard("g1", "0")
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        zero_label = {value: label for label, value in LIMIT_PRESETS}["0"]
        self.assertEqual(labels["tasklimit:g1:0"], f"·{zero_label}·")
        self.assertIn("tasklimit:g1:2M", labels)

    def test_task_limit_chooser_back_button(self):
        kb = task_limit_chooser_keyboard("g1")
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("task:detail:g1", callbacks)

    def test_batch_pending_keyboard(self):
        kb = batch_pending_keyboard("batch1")
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("pending:startall:batch1", callbacks)
        self.assertIn("pending:cancelall:batch1", callbacks)

    def test_stats_period_keyboard_marks_current(self):
        kb = stats_period_keyboard("7")
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["stats:7"], "·7 天·")
        self.assertEqual(labels["stats:0"], "全部")

    def test_send_tg_toggle_label_tracks_setting(self):
        original = settings.auto_send_to_tg
        try:
            settings.auto_send_to_tg = True
            labels = [b.text for row in settings_keyboard().inline_keyboard for b in row]
            self.assertTrue(any("自动发送: ✅" in l for l in labels))
            settings.auto_send_to_tg = False
            labels = [b.text for row in settings_keyboard().inline_keyboard for b in row]
            self.assertTrue(any("自动发送: ❌" in l for l in labels))
        finally:
            settings.auto_send_to_tg = original

    def test_maxsize_chooser_marks_current(self):
        kb = maxsize_chooser_keyboard("1024")
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["setmaxsize:1024"], "·1 GB·")
        self.assertEqual(labels["setmaxsize:0"], "不限")

    def test_cleanup_chooser_marks_current(self):
        kb = cleanup_chooser_keyboard(7)
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["setcleanup:7"], "·7 天·")
        self.assertEqual(labels["setcleanup:0"], "关闭")

    def test_dir_chooser_marks_current_and_uses_index(self):
        kb = dir_chooser_keyboard(["/downloads", "/downloads/movies"])
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("setdir:0", callbacks)
        self.assertIn("setdir:1", callbacks)
        labels = [b.text for row in kb.inline_keyboard for b in row]
        self.assertTrue(any(l.startswith("✅") and "/downloads" in l and "movies" not in l for l in labels))

    def test_notify_toggle_label_tracks_setting(self):
        original = settings.notify_on_complete
        try:
            settings.notify_on_complete = True
            labels = [b.text for row in settings_keyboard().inline_keyboard for b in row]
            self.assertTrue(any("完成通知: ✅" in l for l in labels))
            settings.notify_on_complete = False
            labels = [b.text for row in settings_keyboard().inline_keyboard for b in row]
            self.assertTrue(any("完成通知: ❌" in l for l in labels))
        finally:
            settings.notify_on_complete = original

    def test_concurrent_chooser_marks_current(self):
        kb = concurrent_chooser_keyboard("3")
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["setconcurrent:3"], "·3·")
        self.assertEqual(labels["setconcurrent:5"], "5")


class TestSettingsCard(unittest.TestCase):
    def test_shows_live_values(self):
        original_notify = settings.notify_on_complete
        original_cleanup = settings.auto_cleanup_days
        original_maxsize = settings.max_file_size
        original_sendtg = settings.auto_send_to_tg
        try:
            settings.notify_on_complete = False
            settings.auto_cleanup_days = 7
            settings.max_file_size = 1024 * 1024 * 1024
            settings.auto_send_to_tg = True
            text = render_settings("2097152", "5")
            self.assertIn("最大同时下载：5", text)
            self.assertIn("完成通知：关闭", text)
            self.assertIn("2.0 MiB/s", text)
            self.assertIn("单文件上限：1.0 GiB", text)
            self.assertIn("自动清理已完成：保留 7 天", text)
            self.assertIn("自动发送到 TG：开启", text)
        finally:
            settings.notify_on_complete = original_notify
            settings.auto_cleanup_days = original_cleanup
            settings.max_file_size = original_maxsize
            settings.auto_send_to_tg = original_sendtg

    def test_cleanup_off_shows_disabled(self):
        original = settings.auto_cleanup_days
        try:
            settings.auto_cleanup_days = 0
            self.assertIn("自动清理已完成：关闭", render_settings())
        finally:
            settings.auto_cleanup_days = original

    def test_fmt_max_file_size(self):
        self.assertEqual(_fmt_max_file_size(0), "不限")
        self.assertEqual(_fmt_max_file_size(2 * 1024 * 1024 * 1024), "2.0 GiB")

    def test_maxsize_chooser_shows_current(self):
        self.assertIn("当前：2.0 GiB", render_maxsize_chooser(2 * 1024 * 1024 * 1024))
        self.assertIn("当前：不限", render_maxsize_chooser(0))

    def test_cleanup_chooser_shows_current(self):
        original = settings.auto_cleanup_days
        try:
            settings.auto_cleanup_days = 14
            self.assertIn("当前：保留 14 天", render_cleanup_chooser())
        finally:
            settings.auto_cleanup_days = original

    def test_dir_chooser_hints_when_single_option(self):
        text = render_dir_chooser(["/downloads"])
        self.assertIn("DOWNLOAD_DIR_PRESETS", text)

    def test_dir_chooser_no_hint_with_multiple_options(self):
        text = render_dir_chooser(["/downloads", "/downloads/movies"])
        self.assertNotIn("DOWNLOAD_DIR_PRESETS", text)

    def test_tab_row_marks_selected(self):
        row = list_tab_row("ACTIVE", {"ACTIVE": 2, "COMPLETED": 5})
        labels = {b.callback_data: b.text for b in row}
        self.assertTrue(labels["list:ACTIVE:0"].startswith("·"))
        self.assertFalse(labels["list:COMPLETED:0"].startswith("·"))
        self.assertIn("7", labels["list:ALL:0"])  # ALL = sum of counts

    def test_task_limit_chooser_shows_task_name_and_current(self):
        text = render_task_limit_chooser("movie.mkv", "2097152")
        self.assertIn("movie.mkv", text)
        self.assertIn("2.0 MiB/s", text)


class TestBatchAndStatsCards(unittest.TestCase):
    def test_batch_pending_lists_all_names(self):
        text = render_batch_pending(["a.zip", "b.zip"], duplicate_count=0, overflow_count=0)
        self.assertIn("（2 个待确认）", text)
        self.assertIn("1. a.zip", text)
        self.assertIn("2. b.zip", text)
        self.assertNotIn("已跳过", text)
        self.assertNotIn("超出单次批量上限", text)

    def test_batch_pending_notes_duplicates_and_overflow(self):
        text = render_batch_pending(["a.zip"], duplicate_count=2, overflow_count=3)
        self.assertIn("已跳过 2 个", text)
        self.assertIn("还有 3 条未处理", text)

    def test_batch_pending_escapes_html(self):
        text = render_batch_pending(["<b>x</b>.zip"], duplicate_count=0, overflow_count=0)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;.zip", text)

    def test_render_stats_basic(self):
        stats = {"total": 10, "completed": 6, "failed": 2, "cancelled": 1, "total_bytes": 3 * 1024**3}
        text = render_stats("7 天", stats)
        self.assertIn("7 天", text)
        self.assertIn("新增任务：10", text)
        self.assertIn("完成 6", text)
        self.assertIn("失败 2", text)
        self.assertIn("取消 1", text)
        self.assertIn("66.7%", text)  # 6 / (6+2+1)
        self.assertIn("3.0 GiB", text)

    def test_render_stats_no_finished_tasks_shows_placeholder(self):
        stats = {"total": 0, "completed": 0, "failed": 0, "cancelled": 0, "total_bytes": 0}
        text = render_stats("全部", stats)
        self.assertIn("暂无数据", text)

    def test_render_stats_hints_missing_size_data(self):
        stats = {"total": 3, "completed": 3, "failed": 0, "cancelled": 0, "total_bytes": 0}
        text = render_stats("全部", stats)
        self.assertIn("未记录原始大小", text)


if __name__ == "__main__":
    unittest.main()
