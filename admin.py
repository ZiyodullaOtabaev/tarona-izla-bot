"""
👑 Tarona Izla — Pro Admin Panel 2.0

Bo'limlar:
1. 📊 Chuqur Analitika, 7 kunlik grafik, jonli faollik, CSV/TXT eksport.
2. 🎛 Jonli Boshqaruv (Texnik rejim, platforma kalitlari, parallel oqimlar, anti-spam, reklama imzosi, /start matni).
3. 📢 Pro Rassilka (Fonda ishlash, auditoriya segmenti, Copy/Forward, inline tugmalar, Test, Pauza/Davom/To'xtatish, Tarix).
4. 🔒 Homiy Kanallar 2.0 (Forward orqali qo'shish, tashqi havola, Pauza, obuna limiti, konversiya, Health Check).
5. 👥 Foydalanuvchilar CRM (ID/@username/ism/forward qidiruv, Top-10, Oxirgi 10, Ban ro'yxati, DM xabar, Ban sababi).
6. 🔗 Reklama / UTM Havolalari (?start=ref_... orqali reklama samaradorligini o'lchash).
7. 🖥 Tizim, Xatolar Jurnali, 1-bosishda DB Backup va Kesh boshqaruvi.
8. 👑 Adminlar Boshqaruvi (Ega uchun) va Audit Log.
"""

import asyncio
import re
import shutil
import time
import uuid
from datetime import datetime
from typing import Any

from aiogram import Bot, F, Router, types
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from admin_utils import (
    PLATFORM_LABELS,
    build_markup_from_buttons,
    build_users_csv,
    check_user_subscription,
    create_sqlite_backup,
    esc,
    get_admin_main_kb,
    get_controls_kb,
    get_subscription_keyboard,
    make_bar,
    parse_inline_buttons,
    safe_answer,
    safe_edit,
)
from config import (
    ADMIN_IDS,
    ADMIN_USERNAMES,
    BOT_USERNAME,
    BROADCAST_RATE_LIMIT,
    DEFAULT_SETTINGS,
    DOWNLOAD_DIR,
    PRIMARY_ADMIN_ID,
    is_admin,
    is_owner,
)
from database import db
from runtime import get_system_metrics, notify_concurrency_changed

admin_router = Router()

# ─── FSM HOLATLARI (STATES) ────────────────────────────────────

class AdminStates(StatesGroup):
    # Pro Rassilka
    bc_waiting_content = State()
    bc_configuring = State()
    bc_waiting_buttons = State()

    # Homiy kanallar 2.0
    ch_waiting_tg = State()
    ch_waiting_invite_link = State()
    ch_waiting_ext = State()
    ch_waiting_target = State()

    # Foydalanuvchilar CRM
    usr_waiting_search = State()
    usr_waiting_dm = State()
    usr_waiting_ban_reason = State()

    # Jonli sozlamalar (matn tahrirlash)
    ctrl_waiting_text = State()

    # Reklama / UTM havolalar
    ref_waiting_input = State()

    # Admin qo'shish (Faqat Ega)
    team_waiting_admin = State()


# ─── PRO RASSILKA ENGINE (BACKGROUND TASK) ─────────────────────

AUDIENCE_LABELS = {
    "active": "👥 Barcha tiriklar",
    "24h": "🔥 Oxirgi 24s faollar",
    "7d": "📅 Oxirgi 7 kun faollar",
}

MODE_LABELS = {
    "copy": "📋 Copy (bot nomidan)",
    "forward": "↪️ Forward (aslidek)",
}


class BroadcastManager:
    def __init__(self):
        self.running: bool = False
        self.paused: bool = False
        self.stop_requested: bool = False
        self.task: asyncio.Task | None = None
        self.total: int = 0
        self.processed: int = 0
        self.sent: int = 0
        self.blocked: int = 0
        self.audience: str = "active"
        self.mode: str = "copy"
        self.started_at: float = 0.0
        self.log_id: int | None = None

    def render_progress_text(self) -> str:
        elapsed = max(0.1, time.time() - self.started_at)
        speed = self.processed / elapsed
        remaining = max(0, self.total - self.processed)
        eta_sec = int(remaining / speed) if speed > 0 else 0
        pct = int((self.processed / self.total) * 100) if self.total else 0
        bar = make_bar(self.processed, self.total, width=12)
        state_badge = "⏸ <b>PAUZADA</b>" if self.paused else "⏳ <b>YUBORILMOQDA...</b>"

        return (
            f"📢 <b>Pro Rassilka — Jonli Holat</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Holat: {state_badge}\n"
            f"Auditoriya: <b>{AUDIENCE_LABELS.get(self.audience, self.audience)}</b>\n"
            f"Rejim: <b>{MODE_LABELS.get(self.mode, self.mode)}</b>\n\n"
            f"<code>[{bar}]</code> <b>{pct}%</b>\n\n"
            f"✅ Yetkazildi: <b>{self.sent:,}</b> ta\n"
            f"🚫 Bloklagan/Xato: <b>{self.blocked:,}</b> ta\n"
            f"📊 Jarayon: <b>{self.processed:,} / {self.total:,}</b>\n"
            f"⚡ Tezlik: <b>{speed:.1f} xab/s</b> | Taxminan: <b>{eta_sec} son</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━━━"
        )

    def get_control_kb(self) -> InlineKeyboardMarkup:
        toggle_btn = (
            InlineKeyboardButton(text="▶️ Davom ettirish", callback_data="bc_resume")
            if self.paused
            else InlineKeyboardButton(text="⏸ Pauza", callback_data="bc_pause")
        )
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [toggle_btn, InlineKeyboardButton(text="🛑 To'xtatish", callback_data="bc_stop")],
                [
                    InlineKeyboardButton(text="🔄 Yangilash", callback_data="bc_status"),
                    InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu"),
                ],
            ]
        )

    async def start(
        self,
        bot: Bot,
        admin_id: int,
        admin_name: str,
        from_chat_id: int,
        message_id: int,
        audience: str,
        mode: str,
        buttons_data: list[list[dict[str, str]]],
        status_message: Message,
    ):
        user_ids = await db.get_target_user_ids(audience)
        self.total = len(user_ids)
        if self.total == 0:
            await safe_edit(
                status_message,
                "❌ Tanlangan auditoriyada foydalanuvchilar topilmadi.",
                InlineKeyboardMarkup(
                    inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_broadcast")]]
                ),
            )
            return

        self.running = True
        self.paused = False
        self.stop_requested = False
        self.processed = 0
        self.sent = 0
        self.blocked = 0
        self.audience = audience
        self.mode = mode
        self.started_at = time.time()
        self.log_id = await db.create_broadcast_log(admin_id, audience, mode, self.total)

        await db.log_audit(
            admin_id,
            admin_name,
            "Rassilka boshlandi",
            f"aud={audience}, mode={mode}, total={self.total}",
        )

        await safe_edit(status_message, self.render_progress_text(), self.get_control_kb())

        self.task = asyncio.create_task(
            self._worker(bot, user_ids, from_chat_id, message_id, mode, buttons_data, status_message)
        )

    async def _worker(
        self,
        bot: Bot,
        user_ids: list[int],
        from_chat_id: int,
        message_id: int,
        mode: str,
        buttons_data: list[list[dict[str, str]]],
        status_message: Message,
    ):
        reply_markup = build_markup_from_buttons(buttons_data) if mode == "copy" else None
        last_update = time.time()

        try:
            for uid in user_ids:
                while self.paused and not self.stop_requested:
                    await asyncio.sleep(0.5)

                if self.stop_requested:
                    break

                ok = await self._send_one(bot, uid, from_chat_id, message_id, mode, reply_markup)
                self.processed += 1
                if ok:
                    self.sent += 1
                else:
                    self.blocked += 1

                await asyncio.sleep(1.0 / max(1, BROADCAST_RATE_LIMIT))

                if time.time() - last_update >= 3.0:
                    last_update = time.time()
                    await safe_edit(status_message, self.render_progress_text(), self.get_control_kb())
        finally:
            self.running = False
            self.paused = False
            final_status = "stopped" if self.stop_requested else "completed"
            if self.log_id:
                await db.finish_broadcast_log(self.log_id, self.sent, self.blocked, final_status)

            elapsed = time.time() - self.started_at
            header = (
                "🛑 <b>Rassilka to'xtatildi!</b>"
                if self.stop_requested
                else "🎉 <b>Rassilka muvaffaqiyatli yakunlandi!</b>"
            )
            summary = (
                f"{header}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🎯 Auditoriya: <b>{AUDIENCE_LABELS.get(self.audience, self.audience)}</b>\n"
                f"✅ Yetkazildi: <b>{self.sent:,}</b> ta\n"
                f"🚫 Bloklagan/Xato: <b>{self.blocked:,}</b> ta\n"
                f"👥 Qamrov: <b>{self.processed:,} / {self.total:,}</b>\n"
                f"⏱ Sarflangan vaqt: <b>{elapsed:.1f} soniya</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━"
            )
            kb = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="📜 Tarix", callback_data="bc_history"),
                        InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu"),
                    ]
                ]
            )
            await safe_edit(status_message, summary, kb)

    @staticmethod
    async def _send_one(
        bot: Bot,
        uid: int,
        from_chat_id: int,
        message_id: int,
        mode: str,
        reply_markup: InlineKeyboardMarkup | None,
    ) -> bool:
        async def _dispatch():
            if mode == "forward":
                await bot.forward_message(chat_id=uid, from_chat_id=from_chat_id, message_id=message_id)
            else:
                await bot.copy_message(
                    chat_id=uid,
                    from_chat_id=from_chat_id,
                    message_id=message_id,
                    reply_markup=reply_markup,
                )

        try:
            await _dispatch()
            return True
        except TelegramForbiddenError:
            await db.set_user_active(uid, False)
            return False
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            try:
                await _dispatch()
                return True
            except TelegramForbiddenError:
                await db.set_user_active(uid, False)
                return False
            except Exception:
                return False
        except Exception:
            return False


broadcast_mgr = BroadcastManager()


# ─── ADMIN TEKSHIRUVI VA YORDAMCHILAR ──────────────────────────

def check_admin_access(event: Message | CallbackQuery) -> bool:
    user = event.from_user
    if not user:
        return False
    return is_admin(user.id, user.username)


def _admin_label(event: Message | CallbackQuery) -> str:
    u = event.from_user
    if not u:
        return "Admin"
    return f"@{u.username}" if u.username else (u.first_name or str(u.id))


async def _render_main_dashboard(user_id: int, first_name: str) -> tuple[str, InlineKeyboardMarkup]:
    stats = await db.get_stats()
    metrics = get_system_metrics()
    role = "👑 Asosiy Admin (Ega)" if is_owner(user_id) else "🛠 Admin"
    maint = "🟠 YONIQ (Bot yopiq)" if db.get_bool_setting("maintenance_mode", False) else "🟢 Faol"

    text = (
        f"👑 <b>Tarona Izla — Pro Boshqaruv Markazi 2.0</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Admin: <b>{esc(first_name)}</b> | <code>{role}</code>\n"
        f"🚦 Bot holati: <b>{maint}</b> | ⏱ <code>{metrics['uptime']}</code>\n"
        f"👥 Jami userlar: <b>{stats['total_users']:,}</b> (Bugun: <b>+{stats['today_users']:,}</b>)\n"
        f"📥 Bugungi yuklashlar: <b>{stats['today_downloads']:,}</b> | ⚡ Faol oqim: <b>{metrics['active_jobs']}/{metrics['max_concurrent']}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Kerakli bo'limni tanlang:"
    )
    return text, get_admin_main_kb(user_id, broadcast_mgr.running)


# ═══════════════════════════════════════════════════════════════
#                  0. ASOSIY MENYU (/admin)
# ═══════════════════════════════════════════════════════════════

@admin_router.message(Command("admin"))
async def cmd_admin(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    await state.clear()
    text, kb = await _render_main_dashboard(message.from_user.id, message.from_user.first_name)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_router.callback_query(F.data == "admin_menu")
async def cb_admin_menu(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_main_dashboard(callback.from_user.id, callback.from_user.first_name)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "admin_close")
async def cb_admin_close(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    await callback.answer("Panel yopildi")
    try:
        await callback.message.delete()
    except Exception:
        await safe_edit(callback.message, "✅ Admin paneli yopildi.")


@admin_router.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


# ═══════════════════════════════════════════════════════════════
#             1. CHUQUR ANALITIKA VA EKSPORT
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_stats")
async def cb_admin_stats(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer("Yangilanmoqda...")

    s = await db.get_stats()
    p = s["platform_stats"]
    total = s["total_users"]
    dead = s["dead_users"]
    live = max(0, total - dead)
    live_pct = f"{(live / total * 100):.1f}%" if total > 0 else "100%"

    tot_dl = s["total_downloads"]
    cache_pct = f"{(s['cache_hits'] / tot_dl * 100):.1f}%" if tot_dl > 0 else "0%"

    u_diff = s["today_users"] - s["yesterday_users"]
    u_diff_str = f"+{u_diff}" if u_diff >= 0 else str(u_diff)
    d_diff = s["today_downloads"] - s["yesterday_downloads"]
    d_diff_str = f"+{d_diff}" if d_diff >= 0 else str(d_diff)

    text = (
        "📊 <b>Tarona Izla — Chuqur Analitika</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 <b>Auditoriya:</b>\n"
        f"  • Jami: <b>{total:,}</b> ta\n"
        f"  • Bugun: <b>+{s['today_users']:,}</b> (kechaga nisbatan: <code>{u_diff_str}</code>)\n"
        f"  • DAU (24 soat): <b>{s['active_24h']:,}</b>\n"
        f"  • WAU (7 kun): <b>{s['active_7d']:,}</b> | MAU (30 kun): <b>{s['active_30d']:,}</b>\n"
        f"  • Tirik: <b>{live:,}</b> ({live_pct}) | O'lik: <b>{dead:,}</b> | Ban: <b>{s['banned_users']:,}</b>\n\n"
        f"📥 <b>Yuklashlar:</b>\n"
        f"  • Jami: <b>{tot_dl:,}</b> ta (7 kunda: <b>{s['week_downloads']:,}</b>)\n"
        f"  • Bugun: <b>+{s['today_downloads']:,}</b> (kechaga nisbatan: <code>{d_diff_str}</code>)\n"
        f"  • ⚡ Keshdan berilgan: <b>{s['cache_hits']:,}</b> ({cache_pct})\n\n"
        f"🌐 <b>Platformalar:</b>\n"
        f"  • 📸 Instagram: <b>{p.get('instagram', 0):,}</b>\n"
        f"  • 🎵 TikTok: <b>{p.get('tiktok', 0):,}</b>\n"
        f"  • ▶️ YouTube: <b>{p.get('youtube', 0):,}</b>\n"
        f"  • 🎤 Ovozli (Shazam): <b>{p.get('voice', 0):,}</b>\n"
        f"  • 🔎 Qo'shiq qidiruvi: <b>{p.get('search', 0):,}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📈 7 Kunlik Grafik", callback_data="admin_stats_chart"),
                InlineKeyboardButton(text="⚡ Jonli Faollik", callback_data="admin_stats_live"),
            ],
            [
                InlineKeyboardButton(text="📤 Eksport CSV", callback_data="admin_stats_export_csv"),
                InlineKeyboardButton(text="📄 IDs TXT", callback_data="admin_stats_export_txt"),
            ],
            [
                InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin_stats"),
                InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu"),
            ],
        ]
    )
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "admin_stats_chart")
async def cb_admin_stats_chart(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer()

    days = await db.get_daily_growth(7)
    max_u = max((d["users"] for d in days), default=1)
    max_d = max((d["downloads"] for d in days), default=1)

    lines = ["📈 <b>Oxirgi 7 Kunlik O'sish Grafigi</b>", "━━━━━━━━━━━━━━━━━━━━━━", ""]
    lines.append("👥 <b>Yangi foydalanuvchilar:</b>")
    for d in days:
        bar = make_bar(d["users"], max_u, width=10)
        lines.append(f"<code>{d['label']} | {bar} {d['users']}</code>")

    lines.append("\n📥 <b>Yuklashlar soni:</b>")
    for d in days:
        bar = make_bar(d["downloads"], max_d, width=10)
        lines.append(f"<code>{d['label']} | {bar} {d['downloads']}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin_stats_chart"),
                InlineKeyboardButton(text="⬅️ Analitika", callback_data="admin_stats"),
            ]
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), kb)


@admin_router.callback_query(F.data == "admin_stats_live")
async def cb_admin_stats_live(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer()

    recent = await db.get_recent_downloads(15)
    lines = ["⚡ <b>Jonli Faollik (Oxirgi 15 yuklash)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    if not recent:
        lines.append("\n<i>Hali yuklashlar qayd etilmagan.</i>")
    else:
        for r in recent:
            t = (r["created_at"] or "")[11:16]
            plat = PLATFORM_LABELS.get(r["platform"], r["platform"])
            cache_tag = " ⚡" if r.get("from_cache") else ""
            uname = f"@{esc(r['username'])}" if r.get("username") else esc(r.get("first_name") or r["user_id"])
            detail = f" — <i>{esc(r['detail'][:28])}</i>" if r.get("detail") else ""
            lines.append(f"<code>{t}</code> {plat}{cache_tag} | <b>{uname}</b>{detail}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin_stats_live"),
                InlineKeyboardButton(text="⬅️ Analitika", callback_data="admin_stats"),
            ]
        ]
    )
    await safe_edit(callback.message, "\n".join(lines), kb)


@admin_router.callback_query(F.data == "admin_stats_export_csv")
async def cb_export_csv(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer("CSV tayyorlanmoqda...")

    users = await db.get_all_users_for_export()
    csv_bytes = build_users_csv(users)
    fname = f"tarona_users_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    await callback.message.answer_document(
        BufferedInputFile(csv_bytes, filename=fname),
        caption=f"📤 <b>Foydalanuvchilar bazasi (CSV)</b>\nJami: <b>{len(users):,}</b> ta yozuv.",
        parse_mode=ParseMode.HTML,
    )
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Eksport CSV", f"{len(users)} user")


@admin_router.callback_query(F.data == "admin_stats_export_txt")
async def cb_export_txt(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer("IDs tayyorlanmoqda...")

    ids = await db.get_target_user_ids("active")
    txt_bytes = "\n".join(str(uid) for uid in ids).encode("utf-8")
    fname = f"tarona_active_ids_{datetime.now().strftime('%Y%m%d_%H%M')}.txt"
    await callback.message.answer_document(
        BufferedInputFile(txt_bytes, filename=fname),
        caption=f"📄 <b>Tirik foydalanuvchilar ID ro'yxati</b>\nJami: <b>{len(ids):,}</b> ta ID.",
        parse_mode=ParseMode.HTML,
    )
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Eksport IDs", f"{len(ids)} ID")


# ═══════════════════════════════════════════════════════════════
#         2. JONLI BOSHQARUV (RUNTIME FEATURE FLAGS)
# ═══════════════════════════════════════════════════════════════

def _render_controls_text() -> str:
    ad = db.get_setting("ad_footer", "").strip()
    ad_preview = f"<code>{esc(ad[:60])}</code>" if ad else "<i>O'rnatilmagan</i>"
    return (
        "🎛 <b>Bot Jarayonlarini Jonli Boshqarish</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Barcha o'zgarishlar <b>botni qayta ishga tushirmasdan</b> darhol kuchga kiradi:\n\n"
        f"✍️ Reklama imzosi: {ad_preview}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )


@admin_router.callback_query(F.data == "admin_controls")
async def cb_admin_controls(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    await safe_edit(callback.message, _render_controls_text(), get_controls_kb())
    await callback.answer()


@admin_router.callback_query(F.data.startswith("ctrl_toggle:"))
async def cb_ctrl_toggle(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    key = callback.data.split(":", 1)[1]
    if key not in DEFAULT_SETTINGS:
        await callback.answer("Noma'lum sozlama", show_alert=True)
        return

    cur = db.get_bool_setting(key, key != "maintenance_mode")
    new_val = "0" if cur else "1"
    await db.set_setting(key, new_val)
    await db.log_audit(
        callback.from_user.id,
        _admin_label(callback),
        f"Sozlama: {key}",
        "ON" if new_val == "1" else "OFF",
    )
    await safe_edit(callback.message, _render_controls_text(), get_controls_kb())
    await callback.answer(f"{'🟢 Yoqildi' if new_val == '1' else '🔴 O`chirildi'}")


@admin_router.callback_query(F.data.startswith("ctrl_conc:"))
async def cb_ctrl_conc(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    delta = int(callback.data.split(":", 1)[1])
    cur = db.get_int_setting("max_concurrent", 3)
    new_val = max(1, min(10, cur + delta))
    await db.set_setting("max_concurrent", str(new_val))
    notify_concurrency_changed()
    await db.log_audit(callback.from_user.id, _admin_label(callback), "MAX_CONCURRENT", str(new_val))
    await safe_edit(callback.message, _render_controls_text(), get_controls_kb())
    await callback.answer(f"⚡ Parallel oqimlar: {new_val}")


RATE_STEPS = [0, 3, 5, 10, 15, 20, 30, 50]


@admin_router.callback_query(F.data.startswith("ctrl_rate:"))
async def cb_ctrl_rate(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    delta = int(callback.data.split(":", 1)[1])
    cur = db.get_int_setting("rate_limit_per_min", 10)
    idx = min(range(len(RATE_STEPS)), key=lambda i: abs(RATE_STEPS[i] - cur))
    new_idx = max(0, min(len(RATE_STEPS) - 1, idx + delta))
    new_val = RATE_STEPS[new_idx]
    await db.set_setting("rate_limit_per_min", str(new_val))
    await db.log_audit(
        callback.from_user.id,
        _admin_label(callback),
        "Anti-spam limit",
        "Cheksiz" if new_val == 0 else f"{new_val}/daq",
    )
    await safe_edit(callback.message, _render_controls_text(), get_controls_kb())
    await callback.answer(f"🛡 Limit: {'Cheksiz' if new_val == 0 else f'{new_val}/daq'}")


EDITABLE_TEXT_META = {
    "ad_footer": (
        "✍️ <b>Video/MP3 Reklama Imzosi (Footer)</b>",
        "Har bir yuklangan video va MP3 ostiga qo'shiladigan matnni yuboring (HTML teglar qo'llab-quvvatlanadi).\n"
        "Misol: <code>🔥 Reklama: @sizning_kanalingiz</code>",
    ),
    "welcome_text": (
        "👋 <b>/start Xush Kelibsiz Matni</b>",
        "Yangi /start xabarini yuboring (HTML qo'llab-quvvatlanadi). <code>{name}</code> o'rniga foydalanuvchi ismi qo'yiladi.",
    ),
    "maintenance_text": (
        "🛠 <b>Texnik Ishlar Xabari</b>",
        "Texnik rejim yoqilganda foydalanuvchilarga ko'rinadigan matnni yuboring:",
    ),
}


@admin_router.callback_query(F.data.startswith("ctrl_edit:"))
async def cb_ctrl_edit(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    key = callback.data.split(":", 1)[1]
    if key not in EDITABLE_TEXT_META:
        return

    title, hint = EDITABLE_TEXT_META[key]
    current = db.get_setting(key, "").strip()
    cur_block = f"<b>Hozirgi qiymat:</b>\n<code>{esc(current[:500])}</code>\n\n" if current else "<i>Hozircha standart / bo'sh.</i>\n\n"

    await state.set_state(AdminStates.ctrl_waiting_text)
    await state.update_data(edit_setting_key=key)

    buttons = []
    if current:
        buttons.append([InlineKeyboardButton(text="🗑 Tozalash / Standartga qaytarish", callback_data=f"ctrl_clear:{key}")])
    buttons.append([InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_controls")])

    await safe_edit(
        callback.message,
        f"{title}\n━━━━━━━━━━━━━━━━━━━━━━\n{cur_block}{hint}",
        InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("ctrl_clear:"))
async def cb_ctrl_clear(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    key = callback.data.split(":", 1)[1]
    default_val = DEFAULT_SETTINGS.get(key, "")
    await db.set_setting(key, default_val)
    await state.clear()
    await db.log_audit(callback.from_user.id, _admin_label(callback), f"Matn tozalandi: {key}")
    await callback.answer("✅ Standart holatga qaytarildi!", show_alert=True)
    await safe_edit(callback.message, _render_controls_text(), get_controls_kb())


@admin_router.message(AdminStates.ctrl_waiting_text)
async def process_ctrl_text(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    if not message.text:
        await message.reply("❌ Iltimos, matn ko'rinishida yuboring.")
        return

    data = await state.get_data()
    key = data.get("edit_setting_key")
    new_text = message.text.strip()

    # HTML yaroqliligini tekshirish (buziq HTML teglar bot xabarlarini sindirmasligi uchun)
    try:
        test_preview = new_text.replace("{name}", esc(message.from_user.first_name or "Do'st"))
        preview_msg = await message.reply(
            f"✅ <b>Saqlandi! Ko'rinishi:</b>\n━━━━━━━━━━━━━━━━━━━━━━\n{test_preview}",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bot Boshqaruvi", callback_data="admin_controls")]]
            ),
        )
        _ = preview_msg
    except TelegramBadRequest as e:
        await message.reply(
            f"❌ <b>HTML formatida xato bor!</b>\n<code>{esc(str(e)[:150])}</code>\n\n"
            "Iltimos, teglarni to'g'rilab qayta yuboring (masalan <code>&lt;b&gt;matn&lt;/b&gt;</code>):",
            parse_mode=ParseMode.HTML,
        )
        return

    await db.set_setting(key, new_text)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), f"Matn o'zgartirildi: {key}", new_text[:80])


# ═══════════════════════════════════════════════════════════════
#             3. PRO RASSILKA (SMART BROADCAST 2.0)
# ═══════════════════════════════════════════════════════════════

async def _render_bc_config(data: dict[str, Any]) -> tuple[str, InlineKeyboardMarkup]:
    aud = data.get("bc_audience", "active")
    mode = data.get("bc_mode", "copy")
    btns = data.get("bc_buttons", [])
    count = len(await db.get_target_user_ids(aud))

    btn_summary = f"<b>{sum(len(r) for r in btns)}</b> ta tugma" if btns else "<i>Yo'q</i>"
    note = "\n⚠️ <i>Forward rejimida inline tugmalar qo'shilmaydi.</i>" if (mode == "forward" and btns) else ""

    text = (
        "📢 <b>Pro Rassilka — Sozlash va Tasdiqlash</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎯 Auditoriya: <b>{AUDIENCE_LABELS[aud]}</b> (~<b>{count:,}</b> kishi)\n"
        f"📨 Rejim: <b>{MODE_LABELS[mode]}</b>\n"
        f"🔘 Inline tugmalar: {btn_summary}\n"
        f"⚡ Tezlik: <b>{BROADCAST_RATE_LIMIT} xab/s</b> (fonda ishlaydi){note}\n"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    kb_rows = [
        [
            InlineKeyboardButton(
                text=("✅ " if aud == "active" else "") + "Hammaga",
                callback_data="bc_aud:active",
            ),
            InlineKeyboardButton(
                text=("✅ " if aud == "24h" else "") + "24s faol",
                callback_data="bc_aud:24h",
            ),
            InlineKeyboardButton(
                text=("✅ " if aud == "7d" else "") + "7k faol",
                callback_data="bc_aud:7d",
            ),
        ],
        [
            InlineKeyboardButton(
                text=("✅ " if mode == "copy" else "") + "📋 Copy",
                callback_data="bc_mode:copy",
            ),
            InlineKeyboardButton(
                text=("✅ " if mode == "forward" else "") + "↪️ Forward",
                callback_data="bc_mode:forward",
            ),
        ],
        [
            InlineKeyboardButton(text="➕ Tugma qo'shish", callback_data="bc_add_btn"),
            InlineKeyboardButton(text="🗑 Tugmalarni o'chirish", callback_data="bc_clear_btn"),
        ],
        [
            InlineKeyboardButton(text="🧪 O'zimga test yuborish", callback_data="bc_test"),
        ],
        [
            InlineKeyboardButton(text="🚀 Yuborishni Boshlash", callback_data="bc_start"),
        ],
        [
            InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_menu"),
        ],
    ]
    return text, InlineKeyboardMarkup(inline_keyboard=kb_rows)


@admin_router.callback_query(F.data == "admin_broadcast")
async def cb_admin_broadcast(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()

    if broadcast_mgr.running:
        await safe_edit(callback.message, broadcast_mgr.render_progress_text(), broadcast_mgr.get_control_kb())
        await callback.answer()
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Yangi Rassilka Yaratish", callback_data="bc_new")],
            [InlineKeyboardButton(text="📜 Rassilka Tarixi", callback_data="bc_history")],
            [InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")],
        ]
    )
    await safe_edit(
        callback.message,
        "📢 <b>Pro Rassilka Markazi</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "• Auditoriyani tanlash (Hammaga / 24s / 7 kun)\n"
        "• Copy yoki Forward rejimi + URL tugmalar\n"
        "• Yuborishdan oldin test + fonda Pauza/To'xtatish\n"
        "━━━━━━━━━━━━━━━━━━━━━━",
        kb,
    )
    await callback.answer()


@admin_router.callback_query(F.data == "bc_new")
async def cb_bc_new(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    if broadcast_mgr.running:
        await callback.answer("⚠️ Hozir boshqa rassilka ketmoqda!", show_alert=True)
        return

    await state.set_state(AdminStates.bc_waiting_content)
    await safe_edit(
        callback.message,
        "✍️ <b>Yangi Rassilka — 1-qadam</b>\n\n"
        "Yubormoqchi bo'lgan xabaringizni menga yo'llang.\n"
        "📌 <i>Matn, rasm, video, audio, stiker yoki kanaldan forward qilingan post bo'lishi mumkin.</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_broadcast")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.bc_waiting_content)
async def process_bc_content(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return

    await state.update_data(
        bc_message_id=message.message_id,
        bc_from_chat_id=message.chat.id,
        bc_audience="active",
        bc_mode="copy",
        bc_buttons=[],
    )
    await state.set_state(AdminStates.bc_configuring)
    data = await state.get_data()
    text, kb = await _render_bc_config(data)
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("bc_aud:"), AdminStates.bc_configuring)
async def cb_bc_aud(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    aud = callback.data.split(":", 1)[1]
    await state.update_data(bc_audience=aud)
    text, kb = await _render_bc_config(await state.get_data())
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data.startswith("bc_mode:"), AdminStates.bc_configuring)
async def cb_bc_mode(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    mode = callback.data.split(":", 1)[1]
    await state.update_data(bc_mode=mode)
    text, kb = await _render_bc_config(await state.get_data())
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data == "bc_add_btn", AdminStates.bc_configuring)
async def cb_bc_add_btn(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.set_state(AdminStates.bc_waiting_buttons)
    await safe_edit(
        callback.message,
        "🔘 <b>Inline Tugmalar Qo'shish</b>\n\n"
        "Har bir qatorga bitta tugma yozing:\n"
        "<code>Kanalga kirish | https://t.me/kanal</code>\n\n"
        "Yonma-yon 2 ta tugma uchun <code>&&</code> ishlating:\n"
        "<code>Tugma 1 | https://t.me/a && Tugma 2 | https://t.me/b</code>",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="bc_back_cfg")]]
        ),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "bc_back_cfg")
async def cb_bc_back_cfg(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    data = await state.get_data()
    if "bc_message_id" not in data:
        await cb_admin_broadcast(callback, state)
        return
    await state.set_state(AdminStates.bc_configuring)
    text, kb = await _render_bc_config(data)
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.message(AdminStates.bc_waiting_buttons)
async def process_bc_buttons(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    parsed = parse_inline_buttons(message.text or "")
    if not parsed:
        await message.reply(
            "❌ Tugma formati noto'g'ri!\nMisol: <code>Kanalga o'tish | https://t.me/kanal</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    await state.update_data(bc_buttons=parsed)
    await state.set_state(AdminStates.bc_configuring)
    text, kb = await _render_bc_config(await state.get_data())
    await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_router.callback_query(F.data == "bc_clear_btn", AdminStates.bc_configuring)
async def cb_bc_clear_btn(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.update_data(bc_buttons=[])
    text, kb = await _render_bc_config(await state.get_data())
    await safe_edit(callback.message, text, kb)
    await callback.answer("🗑 Tugmalar o'chirildi")


@admin_router.callback_query(F.data == "bc_test", AdminStates.bc_configuring)
async def cb_bc_test(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not check_admin_access(callback):
        return
    data = await state.get_data()
    mode = data.get("bc_mode", "copy")
    markup = build_markup_from_buttons(data.get("bc_buttons", [])) if mode == "copy" else None

    try:
        if mode == "forward":
            await bot.forward_message(
                chat_id=callback.from_user.id,
                from_chat_id=data["bc_from_chat_id"],
                message_id=data["bc_message_id"],
            )
        else:
            await bot.copy_message(
                chat_id=callback.from_user.id,
                from_chat_id=data["bc_from_chat_id"],
                message_id=data["bc_message_id"],
                reply_markup=markup,
            )
        await callback.answer("🧪 Test xabari yuborildi!", show_alert=False)
    except Exception as e:
        await callback.answer(f"❌ Xato: {str(e)[:120]}", show_alert=True)


@admin_router.callback_query(F.data == "bc_start", AdminStates.bc_configuring)
async def cb_bc_start(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not check_admin_access(callback):
        return
    if broadcast_mgr.running:
        await callback.answer("⚠️ Hozir boshqa rassilka ishlamoqda!", show_alert=True)
        return

    data = await state.get_data()
    await state.clear()
    await callback.answer("🚀 Rassilka fonda boshlandi!")

    await broadcast_mgr.start(
        bot=bot,
        admin_id=callback.from_user.id,
        admin_name=_admin_label(callback),
        from_chat_id=data["bc_from_chat_id"],
        message_id=data["bc_message_id"],
        audience=data.get("bc_audience", "active"),
        mode=data.get("bc_mode", "copy"),
        buttons_data=data.get("bc_buttons", []),
        status_message=callback.message,
    )


@admin_router.callback_query(F.data == "bc_status")
async def cb_bc_status(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    if not broadcast_mgr.running:
        await callback.answer("Faol rassilka yo'q.")
        await cb_admin_broadcast(callback, state)
        return
    await safe_edit(callback.message, broadcast_mgr.render_progress_text(), broadcast_mgr.get_control_kb())
    await callback.answer("Yangilandi")


@admin_router.callback_query(F.data == "bc_pause")
async def cb_bc_pause(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    if broadcast_mgr.running:
        broadcast_mgr.paused = True
        await safe_edit(callback.message, broadcast_mgr.render_progress_text(), broadcast_mgr.get_control_kb())
        await callback.answer("⏸ Rassilka pauzaga qo'yildi")
    else:
        await callback.answer("Faol rassilka yo'q")


@admin_router.callback_query(F.data == "bc_resume")
async def cb_bc_resume(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    if broadcast_mgr.running:
        broadcast_mgr.paused = False
        await safe_edit(callback.message, broadcast_mgr.render_progress_text(), broadcast_mgr.get_control_kb())
        await callback.answer("▶️ Rassilka davom ettirildi")
    else:
        await callback.answer("Faol rassilka yo'q")


@admin_router.callback_query(F.data == "bc_stop")
async def cb_bc_stop(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    if broadcast_mgr.running:
        broadcast_mgr.stop_requested = True
        broadcast_mgr.paused = False
        await callback.answer("🛑 To'xtatish so'rovi yuborildi...", show_alert=True)
    else:
        await callback.answer("Faol rassilka yo'q")


@admin_router.callback_query(F.data == "bc_history")
async def cb_bc_history(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer()
    rows = await db.get_broadcast_history(10)
    lines = ["📜 <b>Rassilka Tarixi (Oxirgi 10 ta)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    if not rows:
        lines.append("\n<i>Hali rassilkalar yuborilmagan.</i>")
    else:
        for r in rows:
            st = {"completed": "✅", "stopped": "🛑", "running": "⏳"}.get(r["status"], "•")
            dt = (r["started_at"] or "")[:16]
            aud = AUDIENCE_LABELS.get(r["audience"], r["audience"])
            lines.append(
                f"{st} <code>{dt}</code> | {aud}\n"
                f"   ✅ <b>{r['sent']:,}</b> / 🚫 <b>{r['blocked']:,}</b> (jami {r['total']:,})"
            )
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    await safe_edit(
        callback.message,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Rassilka", callback_data="admin_broadcast")]]
        ),
    )


# ═══════════════════════════════════════════════════════════════
#             4. HOMIY KANALLAR 2.0 (OPM)
# ═══════════════════════════════════════════════════════════════

async def _render_channels_list() -> tuple[str, InlineKeyboardMarkup]:
    channels = await db.get_channels()
    lines = ["🔒 <b>Homiy Kanallar 2.0 (OPM)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    buttons = [
        [
            InlineKeyboardButton(text="➕ Telegram Kanal", callback_data="ch_add_tg"),
            InlineKeyboardButton(text="🔗 Tashqi Havola", callback_data="ch_add_ext"),
        ]
    ]

    if not channels:
        lines.append("\n<i>Hozirda homiy kanallar qo'shilmagan.</i>")
    else:
        lines.append("")
        for i, ch in enumerate(channels, 1):
            st = "🟢" if ch["is_active"] else "⏸"
            kind = "🔒" if ch.get("check_sub", 1) else "🔗"
            joined = ch.get("joined_count") or 0
            target = ch.get("target_joins") or 0
            cnt_str = f"{joined}/{target}" if target > 0 else f"{joined}"
            lines.append(
                f"{i}. {st}{kind} <b>{esc(ch['title'])}</b> — 👥 <code>{cnt_str}</code>"
            )
            buttons.append([
                InlineKeyboardButton(
                    text=f"{st} {ch['title'][:22]} ({cnt_str})",
                    callback_data=f"ch_view:{ch['id']}",
                )
            ])
        buttons.append([InlineKeyboardButton(text="🩺 Kanallarni Tekshirish (Health)", callback_data="ch_health")])

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data == "admin_channels")
async def cb_admin_channels(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    text, kb = await _render_channels_list()
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data == "ch_add_tg")
async def cb_ch_add_tg(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.set_state(AdminStates.ch_waiting_tg)
    await safe_edit(
        callback.message,
        "➕ <b>Telegram Kanal Qo'shish (Oson usul)</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Quyidagilardan <b>istalgan birini</b> yuboring:\n\n"
        "1️⃣ Kanaldan birorta postni shu yerga <b>Forward</b> qiling\n"
        "2️⃣ Yoki kanal username'ini yuboring: <code>@kanal_username</code>\n"
        "3️⃣ Yoki to'liq yuboring: <code>-100123456 | Nomi | https://t.me/+...</code>\n\n"
        "⚠️ <i>Bot kanalda ADMIN bo'lishi kerak!</i>",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_channels")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.ch_waiting_tg)
async def process_ch_add_tg(message: Message, state: FSMContext, bot: Bot):
    if not check_admin_access(message):
        return

    channel_id: str | None = None
    custom_title: str = ""
    custom_link: str = ""

    # 1. Forward orqali qo'shish (aiogram 3.4+ forward_origin yoki forward_from_chat)
    fwd_chat = getattr(message, "forward_from_chat", None)
    if fwd_chat is None and getattr(message, "forward_origin", None):
        fwd_chat = getattr(message.forward_origin, "chat", None)

    if fwd_chat:
        channel_id = str(fwd_chat.id)
        custom_title = fwd_chat.title or ""
        if getattr(fwd_chat, "username", None):
            custom_link = f"https://t.me/{fwd_chat.username}"
    elif message.text:
        raw = message.text.strip()
        if "|" in raw:
            parts = [p.strip() for p in raw.split("|")]
            channel_id = parts[0]
            if len(parts) > 1:
                custom_title = parts[1]
            if len(parts) > 2:
                custom_link = parts[2]
        else:
            # @username, t.me/username yoki -100...
            m = re.search(r"t\.me/([A-Za-z0-9_]+)$", raw)
            if m and not raw.startswith("https://t.me/+"):
                channel_id = f"@{m.group(1)}"
            else:
                channel_id = raw

    if not channel_id:
        await message.reply("❌ Kanal ma'lumoti aniqlanmadi. Postni forward qiling yoki @username yuboring.")
        return

    # Bot kanalda admin ekanligini va kanal ma'lumotlarini tekshirish
    try:
        chat = await bot.get_chat(channel_id)
        me = await bot.get_me()
        member = await bot.get_chat_member(chat.id, me.id)
        if member.status not in ("administrator", "creator"):
            await message.reply(
                f"⚠️ <b>Bot '{esc(chat.title or channel_id)}' kanalida ADMIN emas!</b>\n"
                "Avval botni kanalga admin qilib qo'shing va qayta urinib ko'ring.",
                parse_mode=ParseMode.HTML,
            )
            return
        resolved_id = str(chat.id)
        title = custom_title or chat.title or str(chat.id)
        invite_link = custom_link
        if not invite_link:
            if chat.username:
                invite_link = f"https://t.me/{chat.username}"
            elif chat.invite_link:
                invite_link = chat.invite_link
    except Exception as e:
        await message.reply(
            f"❌ <b>Kanal topilmadi yoki bot kirish huquqiga ega emas!</b>\n"
            f"Xato: <code>{esc(str(e)[:120])}</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Yopiq kanal bo'lib havola topilmasa, faqat havolani so'raymiz
    if not invite_link:
        await state.update_data(pending_ch_id=resolved_id, pending_ch_title=title)
        await state.set_state(AdminStates.ch_waiting_invite_link)
        await message.reply(
            f"✅ Kanal topildi: <b>{esc(title)}</b> (<code>{resolved_id}</code>)\n\n"
            "Bu yopiq kanal ekan. Foydalanuvchilar kirishi uchun <b>taklif havolasini</b> (<code>https://t.me/+...</code>) yuboring:",
            parse_mode=ParseMode.HTML,
        )
        return

    await db.add_channel(resolved_id, title, invite_link, check_sub=True)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), "Kanal qo'shildi", f"{title} ({resolved_id})")

    await message.reply(
        f"✅ <b>Homiy kanal muvaffaqiyatli qo'shildi!</b>\n\n"
        f"📢 Nomi: <b>{esc(title)}</b>\n"
        f"🆔 ID: <code>{resolved_id}</code>\n"
        f"🔗 Havola: {esc(invite_link)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Kanallar Ro'yxati", callback_data="admin_channels")]]
        ),
    )


@admin_router.message(AdminStates.ch_waiting_invite_link)
async def process_ch_invite_link(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    link = (message.text or "").strip()
    if not link.startswith(("https://t.me/", "http://t.me/", "tg://")):
        await message.reply("❌ To'g'ri Telegram havola yuboring (masalan: <code>https://t.me/+AbCdEf</code>)", parse_mode=ParseMode.HTML)
        return

    data = await state.get_data()
    cid = data["pending_ch_id"]
    title = data["pending_ch_title"]
    await db.add_channel(cid, title, link, check_sub=True)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), "Yopiq kanal qo'shildi", f"{title} ({cid})")

    await message.reply(
        f"✅ <b>Yopiq kanal qo'shildi!</b>\n📢 <b>{esc(title)}</b>\n🔗 {esc(link)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Kanallar Ro'yxati", callback_data="admin_channels")]]
        ),
    )


@admin_router.callback_query(F.data == "ch_add_ext")
async def cb_ch_add_ext(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.set_state(AdminStates.ch_waiting_ext)
    await safe_edit(
        callback.message,
        "🔗 <b>Tashqi Homiy Havola Qo'shish</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Instagram sahifa, YouTube kanal, sayt yoki boshqa bot uchun (bot admin bo'lishi shart emas).\n\n"
        "Format: <code>Tugma Nomi | https://havola</code>\n"
        "Misol: <code>📸 Instagram Sahifamiz | https://instagram.com/tarona</code>",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_channels")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.ch_waiting_ext)
async def process_ch_add_ext(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    parts = [p.strip() for p in (message.text or "").split("|", 1)]
    if len(parts) < 2 or not parts[1].startswith(("http://", "https://", "tg://")):
        await message.reply("❌ Noto'g'ri format! Misol: <code>Homiy nomi | https://instagram.com/...</code>", parse_mode=ParseMode.HTML)
        return

    title, link = parts[0], parts[1]
    ext_id = f"ext_{uuid.uuid4().hex[:8]}"
    await db.add_channel(ext_id, title, link, check_sub=False)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), "Tashqi homiy qo'shildi", f"{title} -> {link}")

    await message.reply(
        f"✅ <b>Tashqi homiy qo'shildi!</b>\n🔗 <b>{esc(title)}</b>\n{esc(link)}",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Kanallar Ro'yxati", callback_data="admin_channels")]]
        ),
    )


async def _render_channel_card(db_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    ch = await db.get_channel_by_db_id(db_id)
    if not ch:
        return None
    st = "🟢 Faol" if ch["is_active"] else "⏸ Pauzada"
    kind = "🔒 Majburiy (Telegram)" if ch.get("check_sub", 1) else "🔗 Tashqi havola"
    joined = ch.get("joined_count") or 0
    target = ch.get("target_joins") or 0
    limit_str = f"<b>{target:,}</b> ta (yetgach avtomatik pauza)" if target > 0 else "<i>Cheksiz</i>"

    text = (
        f"📢 <b>Homiy Kanal Boshqaruvi</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 Nomi: <b>{esc(ch['title'])}</b>\n"
        f"🆔 ID: <code>{esc(ch['channel_id'])}</code>\n"
        f"🔗 Havola: {esc(ch['invite_link'])}\n"
        f"📌 Turi: {kind}\n"
        f"📊 Holati: <b>{st}</b>\n"
        f"👥 O'tgan obunachilar: <b>{joined:,}</b> ta\n"
        f"🎯 Obuna limiti: {limit_str}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )
    toggle_label = "⏸ Pauzaga qo'yish" if ch["is_active"] else "▶️ Qayta yoqish"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=toggle_label, callback_data=f"ch_toggle:{db_id}")],
            [
                InlineKeyboardButton(text="🎯 Limit qo'yish", callback_data=f"ch_target:{db_id}"),
                InlineKeyboardButton(text="🔄 Sanagichni 0 qilish", callback_data=f"ch_reset:{db_id}"),
            ],
            [InlineKeyboardButton(text="🗑 Kanalni O'chirish", callback_data=f"ch_del:{db_id}")],
            [InlineKeyboardButton(text="⬅️ Kanallar Ro'yxati", callback_data="admin_channels")],
        ]
    )
    return text, kb


@admin_router.callback_query(F.data.startswith("ch_view:"))
async def cb_ch_view(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    db_id = int(callback.data.split(":", 1)[1])
    res = await _render_channel_card(db_id)
    if not res:
        await callback.answer("Kanal topilmadi", show_alert=True)
        return
    await safe_edit(callback.message, res[0], res[1])
    await callback.answer()


@admin_router.callback_query(F.data.startswith("ch_toggle:"))
async def cb_ch_toggle(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    db_id = int(callback.data.split(":", 1)[1])
    new_state = await db.toggle_channel(db_id)
    if new_state is None:
        await callback.answer("Kanal topilmadi", show_alert=True)
        return
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Kanal holati", f"id={db_id} -> {'ON' if new_state else 'PAUSED'}")
    res = await _render_channel_card(db_id)
    if res:
        await safe_edit(callback.message, res[0], res[1])
    await callback.answer("🟢 Faollashtirildi" if new_state else "⏸ Pauzaga qo'yildi")


@admin_router.callback_query(F.data.startswith("ch_target:"))
async def cb_ch_target(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    db_id = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.ch_waiting_target)
    await state.update_data(target_ch_db_id=db_id)
    await safe_edit(
        callback.message,
        "🎯 <b>Obuna Limitini Belgilash</b>\n\n"
        "Nechta obunachi o'tgach kanal avtomatik pauza bo'lsin?\n"
        "Raqam yuboring (masalan: <code>500</code>). Cheksiz qilish uchun <code>0</code> yuboring:",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data=f"ch_view:{db_id}")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.ch_waiting_target)
async def process_ch_target(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    raw = (message.text or "").strip()
    if not raw.isdigit():
        await message.reply("❌ Faqat butun son yuboring (masalan: 500 yoki 0).")
        return
    data = await state.get_data()
    db_id = data["target_ch_db_id"]
    target = int(raw)
    await db.set_channel_target(db_id, target)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), "Kanal limiti", f"id={db_id}, target={target}")
    res = await _render_channel_card(db_id)
    if res:
        await message.reply(res[0], parse_mode=ParseMode.HTML, reply_markup=res[1])


@admin_router.callback_query(F.data.startswith("ch_reset:"))
async def cb_ch_reset(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    db_id = int(callback.data.split(":", 1)[1])
    await db.reset_channel_count(db_id)
    res = await _render_channel_card(db_id)
    if res:
        await safe_edit(callback.message, res[0], res[1])
    await callback.answer("🔄 Sanagich 0 ga tushirildi")


@admin_router.callback_query(F.data.startswith("ch_del:"))
async def cb_ch_del(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    db_id = int(callback.data.split(":", 1)[1])
    ch = await db.get_channel_by_db_id(db_id)
    await db.remove_channel_by_db_id(db_id)
    if ch:
        await db.log_audit(callback.from_user.id, _admin_label(callback), "Kanal o'chirildi", ch["title"])
    await callback.answer("🗑 Kanal o'chirildi!", show_alert=True)
    text, kb = await _render_channels_list()
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "ch_health")
async def cb_ch_health(callback: CallbackQuery, bot: Bot):
    if not check_admin_access(callback):
        return
    await callback.answer("Tekshirilmoqda...")

    channels = await db.get_channels()
    me = await bot.get_me()
    lines = ["🩺 <b>Homiy Kanallar — Health Check</b>", "━━━━━━━━━━━━━━━━━━━━━━"]

    for ch in channels:
        if not ch.get("check_sub", 1):
            lines.append(f"🔗 <b>{esc(ch['title'])}</b> — Tashqi havola (tekshirilmaydi)")
            continue
        try:
            member = await bot.get_chat_member(ch["channel_id"], me.id)
            if member.status in ("administrator", "creator"):
                lines.append(f"✅ <b>{esc(ch['title'])}</b> — Bot Admin (OK)")
            else:
                lines.append(f"⚠️ <b>{esc(ch['title'])}</b> — Bot admin EMAS ({member.status})")
        except Exception as e:
            lines.append(f"❌ <b>{esc(ch['title'])}</b> — Xato: <code>{esc(str(e)[:60])}</code>")

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    await safe_edit(
        callback.message,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Qayta tekshirish", callback_data="ch_health"),
                    InlineKeyboardButton(text="⬅️ Kanallar", callback_data="admin_channels"),
                ]
            ]
        ),
    )


# ═══════════════════════════════════════════════════════════════
#             5. FOYDALANUVCHILAR CRM
# ═══════════════════════════════════════════════════════════════

@admin_router.callback_query(F.data == "admin_users")
async def cb_admin_users(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Qidirish (ID / @user / Forward)", callback_data="usr_search")],
            [
                InlineKeyboardButton(text="🏆 Top-10 Faollar", callback_data="usr_top"),
                InlineKeyboardButton(text="🆕 Oxirgi 10 User", callback_data="usr_recent"),
            ],
            [InlineKeyboardButton(text="🚫 Ban Qilinganlar", callback_data="usr_banned")],
            [InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")],
        ]
    )
    await safe_edit(
        callback.message,
        "👥 <b>Foydalanuvchilar CRM</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        "Foydalanuvchilarni qidiring, yuklash tarixini ko'ring, to'g'ridan-to'g'ri xabar yuboring yoki ban qiling:",
        kb,
    )
    await callback.answer()


@admin_router.callback_query(F.data == "usr_search")
async def cb_usr_search(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.set_state(AdminStates.usr_waiting_search)
    await safe_edit(
        callback.message,
        "🔍 <b>Foydalanuvchi Qidirish</b>\n\n"
        "Quyidagilardan birini yuboring:\n"
        "• Telegram <b>ID</b> (masalan: <code>123456789</code>)\n"
        "• <b>@username</b> yoki ism\n"
        "• Yoki foydalanuvchining xabarini shu yerga <b>Forward</b> qiling",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Orqaga", callback_data="admin_users")]]
        ),
    )
    await callback.answer()


async def _render_user_card(user_id: int) -> tuple[str, InlineKeyboardMarkup] | None:
    user = await db.get_user(user_id)
    if not user:
        return None
    breakdown = await db.get_user_download_breakdown(user_id)
    total_dl = sum(breakdown.values()) or user.get("downloads_count", 0)
    plat_parts = [f"{PLATFORM_LABELS.get(k, k)}: <b>{v}</b>" for k, v in breakdown.items()]
    plat_line = ", ".join(plat_parts) if plat_parts else "<i>Hali yuklamagan</i>"

    is_banned = bool(user["is_banned"])
    if is_banned:
        status_str = "🚫 Ban qilingan"
        if user.get("ban_reason"):
            status_str += f" (<i>{esc(user['ban_reason'])}</i>)"
    elif not user.get("is_active", 1):
        status_str = "⚫️ Botni bloklagan (o'lik)"
    else:
        status_str = "🟢 Faol"

    ref_str = f"<code>{esc(user['ref_code'])}</code>" if user.get("ref_code") else "<i>To'g'ridan-to'g'ri</i>"
    uname = f"@{esc(user['username'])}" if user.get("username") else "<i>Yo'q</i>"

    text = (
        f"👤 <b>Foydalanuvchi Kartasi (CRM)</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{user['user_id']}</code>\n"
        f"👤 Ism: <b>{esc(user.get('first_name') or 'Noma`lum')}</b>\n"
        f"🏷 Username: {uname}\n"
        f"📊 Holat: {status_str}\n"
        f"🔗 Manba (UTM): {ref_str}\n"
        f"📅 Qo'shilgan: <code>{user['joined_at']}</code>\n"
        f"⚡ Oxirgi faollik: <code>{user['last_active']}</code>\n\n"
        f"📥 <b>Jami yuklashlar:</b> {total_dl:,} ta\n"
        f"🌐 {plat_line}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━"
    )

    action_row = []
    if is_banned:
        action_row.append(InlineKeyboardButton(text="✅ Bandan chiqarish", callback_data=f"usr_unban:{user_id}"))
    else:
        action_row.append(InlineKeyboardButton(text="🚫 Ban qilish", callback_data=f"usr_ban:{user_id}"))

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💬 Xabar yuborish (DM)", callback_data=f"usr_dm:{user_id}")],
            action_row,
            [
                InlineKeyboardButton(text="🔍 Boshqa qidiruv", callback_data="usr_search"),
                InlineKeyboardButton(text="⬅️ CRM", callback_data="admin_users"),
            ],
        ]
    )
    return text, kb


@admin_router.message(AdminStates.usr_waiting_search)
async def process_usr_search(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return

    # 1. Forward qilingan xabardan user ID olish
    fwd_user = getattr(message, "forward_from", None)
    if fwd_user is None and getattr(message, "forward_origin", None):
        fwd_user = getattr(message.forward_origin, "sender_user", None)

    if fwd_user:
        res = await _render_user_card(fwd_user.id)
        if not res:
            await message.reply(f"❌ User <code>{fwd_user.id}</code> bot bazasida topilmadi.", parse_mode=ParseMode.HTML)
            return
        await state.clear()
        await message.reply(res[0], parse_mode=ParseMode.HTML, reply_markup=res[1])
        return

    if getattr(message, "forward_origin", None) is not None:
        await message.reply("⚠️ Bu foydalanuvchi forward maxfiyligini yoqqan. Iltimos, uning ID yoki @username'ini yozing.")
        return

    query = (message.text or "").strip()
    if not query:
        await message.reply("❌ ID, @username yoki ism yozing.")
        return

    matches = await db.search_users(query, limit=10)
    if not matches:
        await message.reply("❌ Hech qanday foydalanuvchi topilmadi.")
        return

    await state.clear()
    if len(matches) == 1:
        res = await _render_user_card(matches[0]["user_id"])
        if res:
            await message.reply(res[0], parse_mode=ParseMode.HTML, reply_markup=res[1])
        return

    buttons = []
    for u in matches:
        label = f"{u.get('first_name') or 'User'} (@{u['username']})" if u.get("username") else f"{u.get('first_name') or 'User'} [{u['user_id']}]"
        buttons.append([InlineKeyboardButton(text=label[:40], callback_data=f"usr_view:{u['user_id']}")])
    buttons.append([InlineKeyboardButton(text="⬅️ CRM", callback_data="admin_users")])
    await message.reply(
        f"🔍 <b>{len(matches)} ta foydalanuvchi topildi:</b>\nKeraklisini tanlang:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def _render_user_list(title: str, users: list[dict[str, Any]], show_dl: bool = True) -> tuple[str, InlineKeyboardMarkup]:
    lines = [f"<b>{title}</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    buttons = []
    if not users:
        lines.append("\n<i>Ro'yxat bo'sh.</i>")
    else:
        for i, u in enumerate(users, 1):
            uname = f"@{esc(u['username'])}" if u.get("username") else esc(u.get("first_name") or "User")
            extra = f" — 📥 <b>{u.get('downloads_count', 0)}</b>" if show_dl else f" — <code>{(u.get('joined_at') or '')[:16]}</code>"
            lines.append(f"{i}. {uname} (<code>{u['user_id']}</code>){extra}")
            btn_name = f"@{u['username']}" if u.get("username") else (u.get("first_name") or str(u["user_id"]))
            buttons.append([
                InlineKeyboardButton(
                    text=f"{i}. {btn_name[:28]} ({u.get('downloads_count', 0)} 📥)",
                    callback_data=f"usr_view:{u['user_id']}",
                )
            ])
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    buttons.append([InlineKeyboardButton(text="⬅️ CRM", callback_data="admin_users")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data == "usr_top")
async def cb_usr_top(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await safe_answer(callback)
    users = await db.get_top_users(10)
    text, kb = await _render_user_list("🏆 Top-10 Eng Faol Foydalanuvchilar", users, show_dl=True)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "usr_recent")
async def cb_usr_recent(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await safe_answer(callback)
    users = await db.get_recent_users(10)
    text, kb = await _render_user_list("🆕 Oxirgi 10 Qo'shilgan Foydalanuvchi", users, show_dl=False)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "usr_banned")
async def cb_usr_banned(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await safe_answer(callback)
    users = await db.get_banned_users(15)
    text, kb = await _render_user_list("🚫 Ban Qilingan Foydalanuvchilar", users, show_dl=False)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data.startswith("usr_view:"))
async def cb_usr_view(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    uid = int(callback.data.split(":", 1)[1])
    res = await _render_user_card(uid)
    if not res:
        await safe_answer(callback, "Foydalanuvchi topilmadi", show_alert=True)
        return
    await safe_answer(callback)
    await safe_edit(callback.message, res[0], res[1])


@admin_router.callback_query(F.data.startswith("usr_dm:"))
async def cb_usr_dm(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    uid = int(callback.data.split(":", 1)[1])
    await state.set_state(AdminStates.usr_waiting_dm)
    await state.update_data(dm_target_uid=uid)
    await safe_edit(
        callback.message,
        f"💬 <b>Foydalanuvchiga To'g'ridan-to'g'ri Xabar (DM)</b>\n\n"
        f"Qabul qiluvchi: <code>{uid}</code>\n"
        "Yubormoqchi bo'lgan xabaringizni (matn, rasm, video) yozing:",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data=f"usr_view:{uid}")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.usr_waiting_dm)
async def process_usr_dm(message: Message, state: FSMContext, bot: Bot):
    if not check_admin_access(message):
        return
    data = await state.get_data()
    uid = data["dm_target_uid"]
    await state.clear()

    try:
        if message.text:
            await bot.send_message(
                chat_id=uid,
                text=f"📩 <b>Admin xabari:</b>\n\n{esc(message.text)}",
                parse_mode=ParseMode.HTML,
            )
        else:
            await bot.copy_message(chat_id=uid, from_chat_id=message.chat.id, message_id=message.message_id)
        await db.log_audit(message.from_user.id, _admin_label(message), "DM yuborildi", f"user={uid}")
        await message.reply(
            f"✅ Xabar <code>{uid}</code> ga muvaffaqiyatli yetkazildi!",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="👤 Profilga qaytish", callback_data=f"usr_view:{uid}")]]
            ),
        )
    except TelegramForbiddenError:
        await db.set_user_active(uid, False)
        await message.reply("❌ Foydalanuvchi botni bloklagan (xabar yetib bormadi).")
    except Exception as e:
        await message.reply(f"❌ Yuborib bo'lmadi: <code>{esc(str(e)[:120])}</code>", parse_mode=ParseMode.HTML)


@admin_router.callback_query(F.data.startswith("usr_ban:"))
async def cb_usr_ban_prompt(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    uid = int(callback.data.split(":", 1)[1])
    if is_admin(uid):
        await callback.answer("⚠️ Adminni ban qilib bo'lmaydi!", show_alert=True)
        return
    await state.set_state(AdminStates.usr_waiting_ban_reason)
    await state.update_data(ban_target_uid=uid)
    await safe_edit(
        callback.message,
        f"🚫 <b>Foydalanuvchini Ban Qilish</b>\n\n"
        f"User ID: <code>{uid}</code>\n"
        "Ban sababini yozing yoki sababsiz ban qilish tugmasini bosing:",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🚫 Sababsiz Ban Qilish", callback_data=f"usr_ban_quick:{uid}")],
                [InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data=f"usr_view:{uid}")],
            ]
        ),
    )
    await callback.answer()


@admin_router.callback_query(F.data.startswith("usr_ban_quick:"))
async def cb_usr_ban_quick(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    uid = int(callback.data.split(":", 1)[1])
    await state.clear()
    await db.set_user_ban(uid, True, None)
    await db.log_audit(callback.from_user.id, _admin_label(callback), "User ban", f"user={uid}")
    await callback.answer(f"🚫 User {uid} ban qilindi!", show_alert=True)
    res = await _render_user_card(uid)
    if res:
        await safe_edit(callback.message, res[0], res[1])


@admin_router.message(AdminStates.usr_waiting_ban_reason)
async def process_usr_ban_reason(message: Message, state: FSMContext):
    if not check_admin_access(message):
        return
    data = await state.get_data()
    uid = data["ban_target_uid"]
    reason = (message.text or "").strip()[:200]
    await state.clear()
    await db.set_user_ban(uid, True, reason or None)
    await db.log_audit(message.from_user.id, _admin_label(message), "User ban", f"user={uid}, sabab={reason}")
    res = await _render_user_card(uid)
    if res:
        await message.reply(res[0], parse_mode=ParseMode.HTML, reply_markup=res[1])


@admin_router.callback_query(F.data.startswith("usr_unban:"))
async def cb_usr_unban(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    uid = int(callback.data.split(":", 1)[1])
    await db.set_user_ban(uid, False, None)
    await db.log_audit(callback.from_user.id, _admin_label(callback), "User unban", f"user={uid}")
    await callback.answer(f"✅ User {uid} bandan chiqarildi!", show_alert=True)
    res = await _render_user_card(uid)
    if res:
        await safe_edit(callback.message, res[0], res[1])


# ═══════════════════════════════════════════════════════════════
#             6. REKLAMA / UTM HAVOLALARI (DEEP-LINK)
# ═══════════════════════════════════════════════════════════════

_cached_bot_username: str | None = None


async def _get_bot_uname(bot: Bot) -> str:
    global _cached_bot_username
    if _cached_bot_username:
        return _cached_bot_username
    try:
        me = await bot.get_me()
        if me.username:
            _cached_bot_username = me.username
            return _cached_bot_username
    except Exception:
        pass
    return BOT_USERNAME


async def _render_refs_panel(bot: Bot) -> tuple[str, InlineKeyboardMarkup]:
    bot_uname = await _get_bot_uname(bot)

    refs = await db.get_referrals_with_stats()
    lines = [
        "🔗 <b>Reklama Havolalari (UTM / Deep-Link)</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Har bir reklama kanali yoki blogger uchun alohida havola yarating va natijasini kuzating:\n",
    ]
    buttons = [[InlineKeyboardButton(text="➕ Yangi Reklama Havola", callback_data="ref_new")]]

    if not refs:
        lines.append("<i>Hozircha reklama havolalari yaratilmagan.</i>")
    else:
        for i, r in enumerate(refs[:12], 1):
            link = f"https://t.me/{bot_uname}?start=ref_{r['code']}"
            alive = r["alive_users"] or 0
            lines.append(
                f"{i}. <b>{esc(r['title'])}</b>\n"
                f"   🔗 <code>{link}</code>\n"
                f"   👥 Kirdi: <b>{r['joined_users']}</b> | Tirik: <b>{alive}</b> | 📥 Yuklash: <b>{r['total_downloads']}</b>\n"
            )
            buttons.append([
                InlineKeyboardButton(
                    text=f"🗑 O'chirish: {r['title'][:24]}",
                    callback_data=f"ref_del:{r['code']}",
                )
            ])

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data == "admin_refs")
async def cb_admin_refs(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not check_admin_access(callback):
        return
    await safe_answer(callback)
    await state.clear()
    text, kb = await _render_refs_panel(bot)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "ref_new")
async def cb_ref_new(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.set_state(AdminStates.ref_waiting_input)
    await safe_edit(
        callback.message,
        "➕ <b>Yangi Reklama Havolasi Yaratish</b>\n\n"
        "Reklama nomini yuboring (masalan: <code>Uzbek Music Kanali</code>)\n"
        "Yoki o'zingiz kod bering: <code>Uzbek Music | uzmusic</code>",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_refs")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.ref_waiting_input)
async def process_ref_new(message: Message, state: FSMContext, bot: Bot):
    if not check_admin_access(message):
        return
    raw = (message.text or "").strip()
    if not raw:
        await message.reply("❌ Reklama nomini yozing.")
        return

    if "|" in raw:
        title, raw_code = [p.strip() for p in raw.split("|", 1)]
        code = re.sub(r"[^A-Za-z0-9_-]", "", raw_code)[:24]
    else:
        title = raw[:50]
        code = uuid.uuid4().hex[:6]

    if not code:
        code = uuid.uuid4().hex[:6]

    ok = await db.create_referral(code, title, message.from_user.id)
    if not ok:
        await message.reply("❌ Bu kod allaqachon mavjud. Boshqa kod tanlang.")
        return

    await state.clear()
    try:
        bot_uname = (await bot.get_me()).username or BOT_USERNAME
    except Exception:
        bot_uname = BOT_USERNAME

    link = f"https://t.me/{bot_uname}?start=ref_{code}"
    await db.log_audit(message.from_user.id, _admin_label(message), "UTM havola yaratildi", f"{title} ({code})")
    await message.reply(
        f"✅ <b>Reklama havolasi tayyor!</b>\n\n"
        f"🏷 Nomi: <b>{esc(title)}</b>\n"
        f"🔗 Havola (nusxalash uchun bosing):\n<code>{link}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Barcha Havolalar", callback_data="admin_refs")]]
        ),
    )


@admin_router.callback_query(F.data.startswith("ref_del:"))
async def cb_ref_del(callback: CallbackQuery, bot: Bot):
    if not check_admin_access(callback):
        return
    code = callback.data.split(":", 1)[1]
    await db.delete_referral(code)
    await db.log_audit(callback.from_user.id, _admin_label(callback), "UTM havola o'chirildi", code)
    await callback.answer("🗑 Havola o'chirildi")
    text, kb = await _render_refs_panel(bot)
    await safe_edit(callback.message, text, kb)


# ═══════════════════════════════════════════════════════════════
#         7. TIZIM HOLATI, XATOLAR JURNALI VA BACKUP
# ═══════════════════════════════════════════════════════════════

async def _render_system_panel() -> tuple[str, InlineKeyboardMarkup]:
    m = get_system_metrics()
    cache = await db.get_cache_stats()
    errs = await db.get_recent_errors(5)

    jobs_text = ""
    if m["jobs_list"]:
        now = time.time()
        items = [
            f"  • {PLATFORM_LABELS.get(j['platform'], j['platform'])} — <code>{j['user_id']}</code> ({int(now - j['started_at'])}s)"
            for j in m["jobs_list"]
        ]
        jobs_text = "\n🔄 <b>Hozir bajarilmoqda:</b>\n" + "\n".join(items) + "\n"

    ram_line = (
        f"<b>{m['ram_used_gb']:.1f} / {m['ram_total_gb']:.1f} GB</b> ({m['ram_percent']:.0f}%)"
        if m["ram_total_gb"] > 0
        else "<i>Aniqlanmadi</i>"
    )

    text = (
        "🖥 <b>Tizim Holati va Server Monitoringi</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"⏱ Uptime: <b>{m['uptime']}</b>\n"
        f"⚙️ CPU: <b>{m['cpu']}</b>\n"
        f"🧠 RAM: {ram_line}\n"
        f"💾 Disk bo'sh: <b>{m['disk_free_gb']:.1f} / {m['disk_total_gb']:.1f} GB</b> (band: {m['disk_percent']}%)\n\n"
        f"⚡ Faol oqimlar: <b>{m['active_jobs']} / {m['max_concurrent']}</b>\n"
        f"📂 Temp fayllar: <b>{m['downloads_files']}</b> ta (<b>{m['downloads_mb']:.2f} MB</b>)\n"
        f"🗄 Baza (tarona.db): <b>{m['db_mb']:.2f} MB</b>\n"
        f"⚡ Media Kesh: <b>{cache['total']}</b> ta (🎬 {cache['video']} | 🎵 {cache['audio']})\n"
        f"⚠️ Oxirgi xatolar: <b>{len(errs)}</b>+ ta\n"
        f"{jobs_text}"
        "━━━━━━━━━━━━━━━━━━━━━━"
    )

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⚠️ Xatolar Jurnali", callback_data="sys_errors"),
                InlineKeyboardButton(text="💾 Baza Backup (.db)", callback_data="sys_backup"),
            ],
            [
                InlineKeyboardButton(text="🧹 Temp Fayllarni Tozalash", callback_data="sys_clean_dl"),
                InlineKeyboardButton(text="🗑 Media Keshni Tozalash", callback_data="sys_clean_cache_ask"),
            ],
            [
                InlineKeyboardButton(text="🔄 Yangilash", callback_data="admin_system"),
                InlineKeyboardButton(text="♻️ Botni Restart", callback_data="sys_restart"),
            ],
            [
                InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu"),
            ],
        ]
    )
    return text, kb


@admin_router.callback_query(F.data == "admin_system")
async def cb_admin_system(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer("Yangilanmoqda...")
    text, kb = await _render_system_panel()
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "sys_errors")
async def cb_sys_errors(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer()
    errs = await db.get_recent_errors(15)
    lines = ["⚠️ <b>Xatolar Jurnali (Oxirgi 15 ta)</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    if not errs:
        lines.append("\n✅ <i>Xatolar yo'q — bot toza ishlamoqda!</i>")
    else:
        for e in errs:
            dt = (e["created_at"] or "")[5:16]
            u = f" (u:{e['user_id']})" if e.get("user_id") else ""
            lines.append(f"<code>{dt}</code> <b>{esc(e['source'])}</b>{u}\n  ↳ <code>{esc(e['error_text'][:110])}</code>")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    buttons = [[InlineKeyboardButton(text="🔄 Yangilash", callback_data="sys_errors")]]
    if errs:
        buttons[0].append(InlineKeyboardButton(text="🗑 Tozalash", callback_data="sys_errors_clear"))
    buttons.append([InlineKeyboardButton(text="⬅️ Tizim", callback_data="admin_system")])
    await safe_edit(callback.message, "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons))


@admin_router.callback_query(F.data == "sys_errors_clear")
async def cb_sys_errors_clear(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await db.clear_errors()
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Xatolar jurnali tozalandi")
    await callback.answer("🗑 Xatolar jurnali tozalandi!", show_alert=True)
    await cb_sys_errors(callback)


@admin_router.callback_query(F.data == "sys_backup")
async def cb_sys_backup(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer("Zaxira nusxa tayyorlanmoqda...")

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    backup_path = DOWNLOAD_DIR / f"tarona_backup_{stamp}.db"
    try:
        await asyncio.to_thread(create_sqlite_backup, backup_path)
        size_mb = backup_path.stat().st_size / (1024 * 1024)
        await callback.message.answer_document(
            FSInputFile(str(backup_path)),
            caption=f"💾 <b>Tarona Izla — Baza Zaxira Nusxasi</b>\n📅 Vaqt: <code>{stamp}</code>\n📦 Hajmi: <b>{size_mb:.2f} MB</b>",
            parse_mode=ParseMode.HTML,
        )
        await db.log_audit(callback.from_user.id, _admin_label(callback), "DB Backup olindi", f"{size_mb:.2f} MB")
    except Exception as e:
        await callback.message.answer(f"❌ Backup xatosi: <code>{esc(str(e)[:150])}</code>", parse_mode=ParseMode.HTML)
    finally:
        backup_path.unlink(missing_ok=True)


@admin_router.callback_query(F.data == "sys_clean_dl")
async def cb_sys_clean_dl(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    deleted = 0
    freed = 0
    if DOWNLOAD_DIR.exists():
        for item in DOWNLOAD_DIR.iterdir():
            try:
                if item.is_file():
                    freed += item.stat().st_size
                    item.unlink(missing_ok=True)
                    deleted += 1
                elif item.is_dir():
                    for sub in item.rglob("*"):
                        if sub.is_file():
                            freed += sub.stat().st_size
                    shutil.rmtree(item, ignore_errors=True)
                    deleted += 1
            except Exception:
                pass

    mb = freed / (1024 * 1024)
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Temp fayllar tozalandi", f"{deleted} ta, {mb:.2f} MB")
    await callback.answer(f"🧹 {deleted} ta fayl/papka tozalandi ({mb:.2f} MB)", show_alert=True)
    text, kb = await _render_system_panel()
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "sys_clean_cache_ask")
async def cb_sys_clean_cache_ask(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    cache = await db.get_cache_stats()
    await safe_edit(
        callback.message,
        f"🗑 <b>Media Keshni Tozalash</b>\n\n"
        f"Bazadagi <b>{cache['total']}</b> ta tezkor <code>file_id</code> kesh yozuvi o'chiriladi.\n"
        "Takroriy linklar birinchi marta qayta yuklanishi kerak bo'ladi. Tasdiqlaysizmi?",
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Ha, tozalash", callback_data="sys_clean_cache_confirm"),
                    InlineKeyboardButton(text="❌ Bekor qilish", callback_data="admin_system"),
                ]
            ]
        ),
    )
    await callback.answer()


@admin_router.callback_query(F.data == "sys_clean_cache_confirm")
async def cb_sys_clean_cache_confirm(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    cnt = await db.clear_media_cache()
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Media kesh tozalandi", f"{cnt} yozuv")
    await callback.answer(f"🗑 {cnt} ta kesh yozuvi o'chirildi!", show_alert=True)
    text, kb = await _render_system_panel()
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "sys_restart")
async def cb_sys_restart(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("⚠️ Faqat Asosiy Admin (Ega) botni restart qila oladi!", show_alert=True)
        return
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Bot restart qilindi")
    await callback.answer("♻️ Bot qayta ishga tushirilmoqda (2-3 soniya)...", show_alert=True)

    async def _delayed_exit():
        await asyncio.sleep(1.0)
        import os as _os
        _os._exit(0)

    asyncio.create_task(_delayed_exit())


# ═══════════════════════════════════════════════════════════════
#         8. ADMINLAR BOSHQARUVI (RBAC) VA AUDIT LOG
# ═══════════════════════════════════════════════════════════════

async def _render_team_panel(viewer_id: int) -> tuple[str, InlineKeyboardMarkup]:
    dyn_admins = await db.get_dynamic_admins()
    owner_flag = is_owner(viewer_id)

    lines = [
        "👑 <b>Adminlar Jamoasi va Xavfsizlik</b>",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"👑 <b>Asosiy Admin (Ega):</b> <code>{PRIMARY_ADMIN_ID}</code>",
    ]
    static_unames = ", ".join(f"@{u}" for u in ADMIN_USERNAMES)
    if static_unames:
        lines.append(f"🛠 <b>Konfig adminlari:</b> {esc(static_unames)}")

    lines.append("\n📋 <b>Paneldan qo'shilgan adminlar:</b>")
    buttons = []
    if not dyn_admins:
        lines.append("  <i>Hozircha yo'q</i>")
    else:
        for a in dyn_admins:
            uname = f"@{esc(a['username'])}" if a.get("username") else esc(a.get("first_name") or "Admin")
            lines.append(f"  • {uname} (<code>{a['user_id']}</code>)")
            if owner_flag and a["user_id"] != PRIMARY_ADMIN_ID:
                buttons.append([
                    InlineKeyboardButton(
                        text=f"➖ Olib tashlash: {a.get('first_name') or a['user_id']}",
                        callback_data=f"team_del:{a['user_id']}",
                    )
                ])

    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if owner_flag:
        buttons.insert(0, [InlineKeyboardButton(text="➕ Yangi Admin Qo'shish", callback_data="team_add")])
    buttons.append([InlineKeyboardButton(text="📋 Audit Log (Oxirgi 20 harakat)", callback_data="team_audit")])
    buttons.append([InlineKeyboardButton(text="⬅️ Asosiy Menyu", callback_data="admin_menu")])

    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@admin_router.callback_query(F.data == "admin_team")
async def cb_admin_team(callback: CallbackQuery, state: FSMContext):
    if not check_admin_access(callback):
        return
    await state.clear()
    text, kb = await _render_team_panel(callback.from_user.id)
    await safe_edit(callback.message, text, kb)
    await callback.answer()


@admin_router.callback_query(F.data == "team_add")
async def cb_team_add(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        await callback.answer("⚠️ Faqat Asosiy Admin (Ega) admin qo'sha oladi!", show_alert=True)
        return
    await state.set_state(AdminStates.team_waiting_admin)
    await safe_edit(
        callback.message,
        "➕ <b>Yangi Admin Qo'shish</b>\n\n"
        "Yangi adminning <b>Telegram ID</b> raqamini yuboring yoki uning xabarini shu yerga <b>Forward</b> qiling:",
        InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="⬅️ Bekor qilish", callback_data="admin_team")]]
        ),
    )
    await callback.answer()


@admin_router.message(AdminStates.team_waiting_admin)
async def process_team_add(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return

    uid: int | None = None
    uname: str | None = None
    fname: str | None = None

    fwd_user = getattr(message, "forward_from", None)
    if fwd_user is None and getattr(message, "forward_origin", None):
        fwd_user = getattr(message.forward_origin, "sender_user", None)

    if fwd_user:
        uid = fwd_user.id
        uname = fwd_user.username
        fname = fwd_user.first_name
    elif message.text and message.text.strip().isdigit():
        uid = int(message.text.strip())
        u = await db.get_user(uid)
        if u:
            uname = u.get("username")
            fname = u.get("first_name")
    else:
        await message.reply("❌ Raqamli Telegram ID yuboring yoki foydalanuvchi xabarini forward qiling.")
        return

    await db.add_dynamic_admin(uid, uname, fname, message.from_user.id)
    await state.clear()
    await db.log_audit(message.from_user.id, _admin_label(message), "Admin qo'shildi", f"user={uid} (@{uname})")

    text, kb = await _render_team_panel(message.from_user.id)
    await message.reply(f"✅ <code>{uid}</code> adminlar ro'yxatiga qo'shildi!\n\n{text}", parse_mode=ParseMode.HTML, reply_markup=kb)


@admin_router.callback_query(F.data.startswith("team_del:"))
async def cb_team_del(callback: CallbackQuery):
    if not is_owner(callback.from_user.id):
        await callback.answer("⚠️ Faqat Asosiy Admin (Ega) adminni olib tashlay oladi!", show_alert=True)
        return
    uid = int(callback.data.split(":", 1)[1])
    if uid == PRIMARY_ADMIN_ID:
        await callback.answer("⚠️ Asosiy adminni olib tashlab bo'lmaydi!", show_alert=True)
        return
    await db.remove_dynamic_admin(uid)
    await db.log_audit(callback.from_user.id, _admin_label(callback), "Admin olib tashlandi", f"user={uid}")
    await callback.answer(f"➖ Admin {uid} olib tashlandi!", show_alert=True)
    text, kb = await _render_team_panel(callback.from_user.id)
    await safe_edit(callback.message, text, kb)


@admin_router.callback_query(F.data == "team_audit")
async def cb_team_audit(callback: CallbackQuery):
    if not check_admin_access(callback):
        return
    await callback.answer()
    logs = await db.get_audit_logs(20)
    lines = ["📋 <b>Audit Log — Oxirgi 20 Harakat</b>", "━━━━━━━━━━━━━━━━━━━━━━"]
    if not logs:
        lines.append("\n<i>Hozircha harakatlar qayd etilmagan.</i>")
    else:
        for lg in logs:
            dt = (lg["created_at"] or "")[5:16]
            det = f" — <i>{esc(lg['details'][:45])}</i>" if lg.get("details") else ""
            lines.append(f"<code>{dt}</code> <b>{esc(lg['admin_name'])}</b>: {esc(lg['action'])}{det}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")

    await safe_edit(
        callback.message,
        "\n".join(lines),
        InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="🔄 Yangilash", callback_data="team_audit"),
                    InlineKeyboardButton(text="⬅️ Adminlar", callback_data="admin_team"),
                ]
            ]
        ),
    )


__all__ = [
    "admin_router",
    "check_user_subscription",
    "get_subscription_keyboard",
]
