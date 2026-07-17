"""Server status snapshot from /proc — no psutil dependency needed.

Everything here is blocking (file reads + a short sampling sleep), so callers
in async context must run collect_system_status via asyncio.to_thread.
"""
import os
import time

from bot.core.storage import disk_usage_summary


def _cpu_times() -> tuple[int, int]:
    """(total_jiffies, idle_jiffies) from the aggregate cpu line of /proc/stat."""
    with open("/proc/stat", "r", encoding="utf-8") as f:
        parts = [int(x) for x in f.readline().split()[1:]]
    idle = parts[3] + (parts[4] if len(parts) > 4 else 0)  # idle + iowait
    return sum(parts), idle


def _net_bytes() -> tuple[int, int]:
    """(rx_bytes, tx_bytes) summed over all interfaces except loopback."""
    rx = tx = 0
    try:
        with open("/proc/net/dev", "r", encoding="utf-8") as f:
            for line in f.readlines()[2:]:
                iface, _, data = line.partition(":")
                if iface.strip() == "lo" or not data:
                    continue
                fields = data.split()
                rx += int(fields[0])
                tx += int(fields[8])
    except OSError:
        pass
    return rx, tx


def _meminfo() -> dict[str, int]:
    """key -> bytes from /proc/meminfo (values there are in KiB)."""
    info: dict[str, int] = {}
    with open("/proc/meminfo", "r", encoding="utf-8") as f:
        for line in f:
            key, _, rest = line.partition(":")
            fields = rest.split()
            if fields:
                info[key] = int(fields[0]) * 1024
    return info


def _process_rss() -> int:
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) * 1024
    except OSError:
        pass
    return 0


def collect_system_status(download_dir: str, sample_interval: float = 0.3) -> dict:
    """One status snapshot. CPU usage and network speed need two samples, hence
    the short sleep — run this in a thread, never directly on the event loop."""
    total1, idle1 = _cpu_times()
    rx1, tx1 = _net_bytes()
    time.sleep(sample_interval)
    total2, idle2 = _cpu_times()
    rx2, tx2 = _net_bytes()

    dt = max(1, total2 - total1)
    cpu_percent = round(100.0 * (1 - (idle2 - idle1) / dt), 1)

    mem = _meminfo()
    mem_total = mem.get("MemTotal", 0)
    mem_available = mem.get("MemAvailable", 0)
    swap_total = mem.get("SwapTotal", 0)
    swap_free = mem.get("SwapFree", 0)

    with open("/proc/uptime", "r", encoding="utf-8") as f:
        uptime_seconds = float(f.read().split()[0])

    try:
        disk = disk_usage_summary(download_dir)
    except OSError:
        disk = None

    return {
        "cpu_percent": max(0.0, min(100.0, cpu_percent)),
        "cpu_count": os.cpu_count() or 1,
        "load_avg": os.getloadavg(),
        "mem_total": mem_total,
        "mem_used": mem_total - mem_available,
        "mem_percent": round((mem_total - mem_available) / mem_total * 100, 1) if mem_total else 0.0,
        "swap_total": swap_total,
        "swap_used": swap_total - swap_free,
        "uptime_seconds": uptime_seconds,
        "disk": disk,
        "net_rx_total": rx2,
        "net_tx_total": tx2,
        "net_rx_speed": max(0, rx2 - rx1) / sample_interval,
        "net_tx_speed": max(0, tx2 - tx1) / sample_interval,
        "bot_rss": _process_rss(),
    }


def format_uptime(seconds: float) -> str:
    minutes, _ = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}天{hours}小时{minutes}分"
    if hours:
        return f"{hours}小时{minutes}分"
    return f"{minutes}分钟"
