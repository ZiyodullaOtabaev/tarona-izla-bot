"""
Jonli boshqaruv, tizim monitoringi, anti-spam va xatolarni qayd etish moduli.
"""

import asyncio
import ctypes
import html
import os
import shutil
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode

from config import BASE_DIR, DB_PATH, DOWNLOAD_DIR, MAX_CONCURRENT, PRIMARY_ADMIN_ID
from database import db

BOT_START_TIME = time.time()

# Ayni damda ishlayotgan yuklashlar: job_id -> {user_id, platform, detail, started_at}
active_jobs: dict[str, dict[str, Any]] = {}
_slot_condition: asyncio.Condition | None = None

# Anti-spam: user_id -> [timestamp, ...]
_user_requests: dict[int, list[float]] = defaultdict(list)

# Xato bildirishnomalarini cheklash (spam bo'lmasligi uchun): source -> last_alert_ts
_last_error_alert: dict[str, float] = {}

# Majburiy obunadan o'tishni kutayotgan userlar: user_id -> set(channel_id)
pending_sub_channels: dict[int, set[str]] = {}


def _get_condition() -> asyncio.Condition:
    global _slot_condition
    if _slot_condition is None:
        _slot_condition = asyncio.Condition()
    return _slot_condition


def notify_concurrency_changed():
    """Admin MAX_CONCURRENT ni oshirganda kutayotgan vazifalarni uyg'otadi."""
    if _slot_condition is not None:
        async def _wake():
            async with _slot_condition:
                _slot_condition.notify_all()
        try:
            asyncio.get_running_loop().create_task(_wake())
        except RuntimeError:
            pass


def get_max_concurrent() -> int:
    val = db.get_int_setting("max_concurrent", MAX_CONCURRENT)
    return max(1, min(val, 20))


def is_download_queue_busy() -> bool:
    return len(active_jobs) >= get_max_concurrent()


@asynccontextmanager
async def acquire_download_slot(user_id: int, platform: str, detail: str = ""):
    """
    Dinamik parallel yuklash chegarasi.
    Admin paneldan MAX_CONCURRENT o'zgartirilsa darhol kuchga kiradi.
    """
    cond = _get_condition()
    job_id = uuid.uuid4().hex[:8]

    async with cond:
        while len(active_jobs) >= get_max_concurrent():
            await cond.wait()
        active_jobs[job_id] = {
            "user_id": user_id,
            "platform": platform,
            "detail": detail[:60],
            "started_at": time.time(),
        }

    try:
        yield job_id
    finally:
        async with cond:
            active_jobs.pop(job_id, None)
            cond.notify_all()


def check_rate_limit(user_id: int) -> tuple[bool, int]:
    """
    Foydalanuvchi uchun daqiqalik anti-spam tekshiruvi.
    Returns: (ruxsat_bor_mi, qolgan_soniya)
    """
    limit = db.get_int_setting("rate_limit_per_min", 10)
    if limit <= 0:
        return True, 0

    now = time.time()
    window = 60.0
    stamps = [t for t in _user_requests[user_id] if now - t < window]

    if len(stamps) >= limit:
        _user_requests[user_id] = stamps
        wait_sec = max(1, int(window - (now - stamps[0])))
        return False, wait_sec

    stamps.append(now)
    _user_requests[user_id] = stamps

    # Xotirani vaqti-vaqti bilan tozalash
    if len(_user_requests) > 5000:
        stale = [uid for uid, ts in _user_requests.items() if not ts or now - ts[-1] > window]
        for uid in stale:
            _user_requests.pop(uid, None)

    return True, 0


def format_caption_with_footer(base_caption: str) -> str:
    """Admin paneldan kiritilgan reklama/imzo matnini xavfsiz qo'shadi."""
    footer = db.get_setting("ad_footer", "").strip()
    if not footer:
        return base_caption[:1024]

    combined = f"{base_caption}\n\n{footer}"
    if len(combined) <= 1024:
        return combined
    return base_caption[:1024]


async def report_error(
    bot: Bot | None,
    source: str,
    error_text: str,
    user_id: int | None = None,
    critical: bool = False,
):
    """Xatoni bazaga yozadi va yoqilgan bo'lsa asosiy adminga xabar yuboradi."""
    clean_err = str(error_text).strip()
    print(f"[!] [{source}] (user={user_id}): {clean_err[:300]}")

    try:
        await db.log_error(source, clean_err, user_id)
    except Exception:
        pass

    if not critical or bot is None:
        return
    if not db.get_bool_setting("error_alerts", True):
        return

    now = time.time()
    if now - _last_error_alert.get(source, 0) < 45:
        return
    _last_error_alert[source] = now

    try:
        safe_src = html.escape(source)
        safe_err = html.escape(clean_err[:350])
        u_line = f"\n👤 User ID: <code>{user_id}</code>" if user_id else ""
        await bot.send_message(
            chat_id=PRIMARY_ADMIN_ID,
            text=(
                f"⚠️ <b>Bot xatolik bildirishnomasi</b>\n\n"
                f"📍 Manba: <code>{safe_src}</code>{u_line}\n"
                f"💬 Xato: <code>{safe_err}</code>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        pass


def format_uptime(seconds: float) -> str:
    total = int(max(0, seconds))
    days, rem = divmod(total, 86400)
    hours, rem = divmod(rem, 3600)
    minutes, secs = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days}k")
    if hours:
        parts.append(f"{hours}s")
    if minutes:
        parts.append(f"{minutes}d")
    parts.append(f"{secs}son")
    return " ".join(parts)


def _get_ram_info() -> dict[str, Any]:
    """Cross-platform RAM ko'rsatkichlari (qo'shimcha kutubxonasiz)."""
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        return {
            "used_gb": vm.used / (1024 ** 3),
            "total_gb": vm.total / (1024 ** 3),
            "percent": vm.percent,
        }
    except Exception:
        pass

    if os.name == "nt":
        try:
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            total_gb = stat.ullTotalPhys / (1024 ** 3)
            used_gb = (stat.ullTotalPhys - stat.ullAvailPhys) / (1024 ** 3)
            return {
                "used_gb": used_gb,
                "total_gb": total_gb,
                "percent": float(stat.dwMemoryLoad),
            }
        except Exception:
            pass
    else:
        try:
            meminfo = {}
            with open("/proc/meminfo", "r", encoding="utf-8") as f:
                for line in f:
                    parts = line.split(":")
                    if len(parts) == 2:
                        meminfo[parts[0].strip()] = int(parts[1].strip().split()[0])
            total_kb = meminfo.get("MemTotal", 0)
            avail_kb = meminfo.get("MemAvailable", meminfo.get("MemFree", 0))
            if total_kb > 0:
                used_kb = total_kb - avail_kb
                return {
                    "used_gb": used_kb / (1024 ** 2),
                    "total_gb": total_kb / (1024 ** 2),
                    "percent": round(used_kb / total_kb * 100, 1),
                }
        except Exception:
            pass

    return {"used_gb": 0.0, "total_gb": 0.0, "percent": 0.0}


def _get_cpu_info() -> str:
    cores = os.cpu_count() or 1
    try:
        import psutil  # type: ignore
        pct = psutil.cpu_percent(interval=0.1)
        return f"{pct:.0f}% ({cores} yadro)"
    except Exception:
        pass

    if hasattr(os, "getloadavg"):
        try:
            load1, _, _ = os.getloadavg()
            return f"Load {load1:.2f} ({cores} yadro)"
        except Exception:
            pass

    return f"{cores} yadro"


def _dir_size_and_count(directory: Path) -> tuple[int, int]:
    total_bytes = 0
    count = 0
    if directory.exists():
        for item in directory.rglob("*"):
            if item.is_file():
                try:
                    total_bytes += item.stat().st_size
                    count += 1
                except OSError:
                    pass
    return total_bytes, count


def get_system_metrics() -> dict[str, Any]:
    """Server va botning joriy tizim ko'rsatkichlari."""
    ram = _get_ram_info()
    disk = shutil.disk_usage(BASE_DIR)
    dl_bytes, dl_files = _dir_size_and_count(DOWNLOAD_DIR)
    db_bytes = DB_PATH.stat().st_size if DB_PATH.exists() else 0

    return {
        "uptime": format_uptime(time.time() - BOT_START_TIME),
        "cpu": _get_cpu_info(),
        "ram_used_gb": ram["used_gb"],
        "ram_total_gb": ram["total_gb"],
        "ram_percent": ram["percent"],
        "disk_free_gb": disk.free / (1024 ** 3),
        "disk_total_gb": disk.total / (1024 ** 3),
        "disk_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0.0,
        "downloads_mb": dl_bytes / (1024 * 1024),
        "downloads_files": dl_files,
        "db_mb": db_bytes / (1024 * 1024),
        "active_jobs": len(active_jobs),
        "max_concurrent": get_max_concurrent(),
        "jobs_list": list(active_jobs.values()),
    }
