import unittest

from bot.core.cards import render_server_status
from bot.core.keyboards import server_status_keyboard, settings_keyboard
from bot.core.sysinfo import collect_system_status, format_uptime


class FakeStats:
    num_active = 2
    num_waiting = 1

    def download_speed_string(self):
        return "1.0 MiB/s"

    def upload_speed_string(self):
        return "0 B/s"


def fake_info(**overrides):
    info = {
        "cpu_percent": 12.5,
        "cpu_count": 4,
        "load_avg": (0.52, 0.48, 0.40),
        "mem_total": 4 * 1024**3,
        "mem_used": 1 * 1024**3,
        "mem_percent": 25.0,
        "swap_total": 0,
        "swap_used": 0,
        "uptime_seconds": 3 * 86400 + 4 * 3600 + 12 * 60,
        "disk": {"total": "50.0GB", "used": "20.0GB", "free": "30.0GB", "percent_used": 40.0},
        "net_rx_total": 10 * 1024**3,
        "net_tx_total": 2 * 1024**3,
        "net_rx_speed": 2 * 1024**2,
        "net_tx_speed": 512 * 1024,
        "bot_rss": 85 * 1024**2,
    }
    info.update(overrides)
    return info


class TestCollect(unittest.TestCase):
    def test_returns_all_expected_keys(self):
        info = collect_system_status("/tmp", sample_interval=0.05)
        for key in fake_info():
            self.assertIn(key, info)
        self.assertGreaterEqual(info["cpu_percent"], 0.0)
        self.assertLessEqual(info["cpu_percent"], 100.0)
        self.assertGreater(info["mem_total"], 0)
        self.assertGreater(info["uptime_seconds"], 0)


class TestFormatUptime(unittest.TestCase):
    def test_ranges(self):
        self.assertEqual(format_uptime(59), "0分钟")
        self.assertEqual(format_uptime(3 * 3600 + 5 * 60), "3小时5分")
        self.assertEqual(format_uptime(2 * 86400 + 3600), "2天1小时0分")


class TestRender(unittest.TestCase):
    def test_full_card(self):
        text = render_server_status(fake_info(), FakeStats())
        for expected in ("服务器状态", "已运行 3天4小时12分", "CPU 12.5%（4 核）",
                         "负载 0.52 / 0.48 / 0.40", "内存 1.0 GiB / 4.0 GiB（25.0%）",
                         "剩余 30.0GB", "aria2", "机器人内存 85.0 MiB"):
            self.assertIn(expected, text)
        self.assertNotIn("交换", text)  # swap_total == 0 hides the swap line

    def test_degrades_without_disk_and_stats(self):
        text = render_server_status(fake_info(disk=None, swap_total=2 * 1024**3, swap_used=1024**3), None)
        self.assertNotIn("磁盘", text)
        self.assertNotIn("aria2", text)
        self.assertIn("交换 1.0 GiB / 2.0 GiB", text)


class TestKeyboards(unittest.TestCase):
    def test_settings_has_sysinfo_entry(self):
        callbacks = [b.callback_data for row in settings_keyboard().inline_keyboard for b in row]
        self.assertIn("admin:sysinfo", callbacks)

    def test_status_keyboard_refresh_and_back(self):
        callbacks = [b.callback_data for row in server_status_keyboard().inline_keyboard for b in row]
        self.assertEqual(callbacks, ["admin:sysinfo", "nav:settings"])


if __name__ == "__main__":
    unittest.main()
