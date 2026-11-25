"""
Admin Panel 2.0 uchun yordamchi funksiyalar, klaviaturalar, OPM 2.0 va eksport vositalari.
"""

import asyncio
import csv
import html
import io
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message

from config import DB_PATH, DOWNLOAD_DIR, PRIMARY_ADMIN_ID, is_admin, is_owner
from database import db
from runtime import pending_sub_channels

# Tashqi homiy havolalari (check_sub=0) uchun o'tgan userlar: (user_id, channel_id)
_external_passed: set[tuple[int, str]] = set()
# Tasdiqlangan obunalar keshi (60 soniya): (user_id, channels_sig) -> expire_ts
_sub_verified_cache: dict[tuple[int, str], float] = {}
SUB_CACHE_TTL = 60.0

PLATFORM_LABELS = {
    "instagram": "📸 Instagram",
    "tiktok": "🎵 TikTok",
    "youtube": "▶️ YouTube",
    "voice": "🎤 Ovozli",
    "search": "🔎 Qidiruv",
}


def esc(text: Any) -> str:
    """HTML xavfsiz qochirish (escaping)."""
    return html.escape(str(text or ""))


def make_bar(value: int, max_val: int, width: int = 8) -> str:
    """ASCII progress/grafik chizig'i."""
    if max_val <= 0 or value <= 0:
        return "░" * width
    filled = max(1, min(width, round((value / max_val) * width)))
    return "█" * filled + "░" * (width - filled)


async def safe_edit(
    message: Message,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    parse_mode: ParseMode = ParseMode.HTML,
    disable_web_page_preview: bool = True,
):
    """'message is not modified' xatosini yutib, xabarni xavfsiz yangilaydi."""
    try:
        await message.edit_text(
            text=text,
            parse_mode=parse_mode,
            reply_markup=reply_markup,
            disable_web_page_preview=disable_web_page_preview,
        )
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return
        try:
            await message.answer(
                text=text,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
                disable_web_page_preview=disable_web_page_preview,
            )
        except Exception:
            pass


def parse_inline_buttons(raw_text: str) -> list[list[dict[str, str]]]:
    """
    Har bir qatordan `Matn | https://...` ko'rinishidagi tugmalarni ajratadi.
    Bitta qatorda `&&` bilan ikkita tugma yonma-yon qo'yish ham mumkin.
    """
    rows: list[list[dict[str, str]]] = []
    for line in raw_text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        row: list[dict[str, str]] = []
        for chunk in line.split("&&"):
            parts = [p.strip() for p in chunk.split("|", 1)]
            if len(parts) == 2 and parts[0] and parts[1].startswith(("http://", "https://", "tg://")):
                row.append({"text": parts[0][:64], "url": parts[1]})
        if row:
            rows.append(row)
    return rows


def build_markup_from_buttons(rows: list[list[dict[str, str]]]) -> InlineKeyboardMarkup | None:
    if not rows:
        return None
    kb_rows = [
        [InlineKeyboardButton(text=btn["text"], url=btn["url"]) for btn in row]
        for row in rows
        if row
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb_rows) if kb_rows else None


def get_admin_main_kb(user_id: int, broadcast_running: bool = False) -> InlineKeyboardMarkup:
    """Admin panelining asosiy 2-ustunli menyusi."""
    b_label = "📢 Rassilka (🔴 FAOL)" if broadcast_running else "📢 Pro Rassilka"
    maint = db.get_bool_setting("maintenance_mode", False)
    ctrl_label = "🎛 Boshqaruv (🛠 TEXNIK)" if maint else "🎛 Bot Boshqaruvi"

    rows = [
        [
            InlineKeyboardButton(text="📊 Analitika", callback_data="admin_stats"),
            InlineKeyboardButton(text=b_label, callback_data="admin_broadcast"),
        ],
        [
            InlineKeyboardButton(text=ctrl_label, callback_data="admin_controls"),
            InlineKeyboardButton(text="🔒 Homiy Kanallar", callback_data="admin_channels"),
        ],
        [
            InlineKeyboardButton(text="👥 Foydalanuvchilar", callback_data="admin_users"),
            InlineKeyboardButton(text="🔗 Reklama Havolalar", callback_data="admin_refs"),
        ],
        [
            InlineKeyboardButton(text="🖥 Tizim va Xatolar", callback_data="admin_system"),
            InlineKeyboardButton(
                text="👑 Adminlar & Audit" if is_owner(user_id) else "📋 Audit Log",
                callback_data="admin_team",
            ),
        ],
        [
            InlineKeyboardButton(text="❌ Panelni Yopish", callback_data="admin_close"),
        ],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def get_controls_kb() -> InlineKeyboardMarkup:
    """Jonli boshqaruv (Feature Flags & Runtime Settings) tugmalari."""
    def icon(key: str, default: bool = True) -> str:
        return "🟢" if db.get_bool_setting(key, default) else "🔴"

    maint_on = db.get_bool_setting("maintenance_mode", False)
    maint_icon = "🟠 YONIQ" if maint_on else "⚪️ O'CHIQ"
    conc = db.get_int_setting("max_concurrent", 3)
    rate = db.get_int_setting("rate_limit_per_min", 10)
    rate_txt = "Cheksiz" if rate <= 0 else f"{rate}/daq"
    has_ad = bool(db.get_setting("ad_footer", "").strip())
    has_welcome = bool(db.get_setting("welcome_text", "").strip())

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"🛠 Texnik rejim: {maint_icon}",
                    callback_data="ctrl_toggle:maintenance_mode",
                )
            ],
            [
                InlineKeyboardButton(
                    text=f"{icon('enable_instagram')} Instagram",
                    callback_data="ctrl_toggle:enable_instagram",
                ),
                InlineKeyboardButton(
                    text=f"{icon('enable_tiktok')} TikTok",
                    callback_data="ctrl_toggle:enable_tiktok",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{icon('enable_youtube')} YouTube",
                    callback_data="ctrl_toggle:enable_youtube",
                ),
                InlineKeyboardButton(
                    text=f"{icon('enable_voice')} Ovozli (Shazam)",
                    callback_data="ctrl_toggle:enable_voice",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{icon('enable_search')} Qo'shiq qidiruvi",
                    callback_data="ctrl_toggle:enable_search",
                ),
                InlineKeyboardButton(
                    text=f"{icon('cache_enabled')} Media Kesh",
                    callback_data="ctrl_toggle:cache_enabled",
                ),
            ],
            [
                InlineKeyboardButton(
                    text=f"{icon('error_alerts')} Xato bildirishnomalari",
                    callback_data="ctrl_toggle:error_alerts",
                ),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data="ctrl_conc:-1"),
                InlineKeyboardButton(text=f"⚡ Oqimlar: {conc}", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data="ctrl_conc:+1"),
            ],
            [
                InlineKeyboardButton(text="➖", callback_data="ctrl_rate:-1"),
                InlineKeyboardButton(text=f"🛡 Limit: {rate_txt}", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data="ctrl_rate:+1"),
            ],
            [
                InlineKeyboardButton(
                    text=f"✍️ Reklama Imzosi {'✅' if has_ad else '➕'}",
                    callback_data="ctrl_edit:ad_footer",
                ),
                InlineKeyboardButton(
                    text=f"👋 /start Matni {'✅' if has_welcome else '✏️'}",
                    callback_data="ctrl_edit:welcome_text",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="📝 Texnik Rejim Matnini Tahrirlash",
                    callback_data="ctrl_edit:maintenance_text",
                )
            ],
            [InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")],
        ]
    )


def create_sqlite_backup(dest_path: Path) -> Path:
    """SQLite WAL bazasidan xavfsiz va to'liq zaxira nusxa (snapshot) yaratadi."""
    src = sqlite3.connect(str(DB_PATH))
    dst = sqlite3.connect(str(dest_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_path


def build_users_csv(users: list[dict[str, Any]]) -> bytes:
    """Foydalanuvchilar ro'yxatidan UTF-8 BOM CSV fayl yaratadi (Excel'da to'g'ri ochiladi)."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow([
        "user_id",
        "username",
        "first_name",
        "joined_at",
        "last_active",
        "is_active",
        "is_banned",
        "ref_code",
        "downloads_count",
    ])
    for u in users:
        writer.writerow([
            u["user_id"],
            u.get("username") or "",
            u.get("first_name") or "",
            u.get("joined_at") or "",
            u.get("last_active") or "",
            u.get("is_active", 1),
            u.get("is_banned", 0),
            u.get("ref_code") or "",
            u.get("downloads_count", 0),
        ])
    return ("\ufeff" + buf.getvalue()).encode("utf-8")


# ═══════════════════════════════════════════════════════════════
#         MAJBURIY OBUNANI TEKSHIRISH (OPM 2.0 HELPER)
# ═══════════════════════════════════════════════════════════════

async def check_user_subscription(bot: Bot, user_id: int, from_check_button: bool = False) -> tuple[bool, list[dict]]:
    """
    Foydalanuvchi barcha faol homiy kanallarga a'zo bo'lganligini PARALLEL tekshiradi.
    Tasdiqlangan obuna 60 soniya xotirada keshlanadi (0ms takroriy tekshiruv).
    """
    channels = await db.get_active_channels()
    if not channels:
        return True, []

    sig = ",".join(str(ch["channel_id"]) for ch in channels)
    now = time.time()
    if not from_check_button and _sub_verified_cache.get((user_id, sig), 0.0) > now:
        return True, []

    async def _check_one(ch: dict) -> dict | None:
        ch_id = ch["channel_id"]
        if not ch.get("check_sub", 1):
            if from_check_button:
                _external_passed.add((user_id, ch_id))
                return None
            return None if (user_id, ch_id) in _external_passed else ch
        try:
            member = await bot.get_chat_member(chat_id=ch_id, user_id=user_id)
            if member.status in ["left", "kicked"]:
                return ch
        except Exception:
            pass
        return None

    results = await asyncio.gather(*(_check_one(ch) for ch in channels))
    missing = [ch for ch in results if ch is not None]

    if missing:
        pending_sub_channels[user_id] = {ch["channel_id"] for ch in missing}
    else:
        _sub_verified_cache[(user_id, sig)] = now + SUB_CACHE_TTL
        if len(_sub_verified_cache) > 5000:
            expired = [k for k, exp in _sub_verified_cache.items() if exp <= now]
            for k in expired:
                _sub_verified_cache.pop(k, None)

        prev_missing = pending_sub_channels.pop(user_id, set())
        for ch_id in prev_missing:
            auto_paused = await db.increment_channel_join(ch_id)
            if auto_paused:
                ch_info = await db.get_channel(ch_id)
                title = esc(ch_info["title"]) if ch_info else esc(ch_id)
                target = ch_info["target_joins"] if ch_info else 0
                try:
                    await bot.send_message(
                        chat_id=PRIMARY_ADMIN_ID,
                        text=(
                            f"🎯 <b>Homiy kanal limitga yetdi!</b>\n\n"
                            f"📢 Kanal: <b>{title}</b>\n"
                            f"👥 Obunachilar: <b>{target}</b> ta\n\n"
                            f"Kanal avtomatik ravishda ⏸ <b>Pauza</b> holatiga o'tkazildi."
                        ),
                        parse_mode=ParseMode.HTML,
                    )
                except Exception:
                    pass

    return len(missing) == 0, missing


def get_subscription_keyboard(missing_channels: list[dict]) -> InlineKeyboardMarkup:
    """Homiy kanallarga obuna bo'lish tugmalari."""
    buttons = []
    for i, ch in enumerate(missing_channels, 1):
        title = ch.get("title") or f"{i}-kanal"
        icon = "📢" if ch.get("check_sub", 1) else "🔗"
        buttons.append([
            InlineKeyboardButton(text=f"{icon} {title[:32]}", url=ch["invite_link"])
        ])
    buttons.append([
        InlineKeyboardButton(text="✅ A'zo bo'ldim (Tekshirish)", callback_data="check_subscription")
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)
