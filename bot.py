"""
🎵 Tarona Izla — Telegram Bot (Turbo Parallel Engine)

Imkoniyatlar:
- Instagram Reels, TikTok, YouTube'dan video va MP3 audioni maksimal tezlikda yuklab berish.
- Parallel konveyer: Telegram Direct CDN + audio-only DASH + Shazam + MP3 bir vaqtda ishlaydi.
- Takroriy havolalar SQLite file_id keshidan 0.1 soniyada uzatiladi.
- 👑 Pro Admin Panel 2.0 (/admin): jonli boshqaruv, analitika, UTM, Pro Rassilka, OPM 2.0, CRM, Backup.
"""

import asyncio
import html
import shutil
import uuid
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from admin import admin_router, check_user_subscription, get_subscription_keyboard
from config import (
    BOT_TOKEN,
    DOWNLOAD_DIR,
    LOW_RAM_MODE,
    PRIMARY_ADMIN_ID,
    WEB_HOST,
    WEB_PORT,
    WEBHOOK_HOST,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    is_admin,
)
from database import db
from downloader import Downloader, ExtractedMedia
from recognizer import MusicRecognizer, MusicResult
from runtime import (
    acquire_download_slot,
    check_rate_limit,
    format_caption_with_footer,
    is_download_queue_busy,
    report_error,
)

# ─── Bot va Dispatcher ─────────────────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# Avval Admin routerni ulaymiz (ustuvorligi yuqori)
dp.include_router(admin_router)

# Asosiy router
main_router = Router()
dp.include_router(main_router)

recognizer = MusicRecognizer()

# Qidiruv so'rovlarini saqlash (To'liq MP3 yuklash uchun)
track_search_cache: dict[str, str] = {}
# Qo'shiq nomi bo'yicha qidiruv natijalari keshi (session_id -> list of tracks)
search_results_cache: dict[str, list[dict]] = {}

PLATFORM_NAME = {
    "instagram": "📸 Instagram",
    "youtube": "▶️ YouTube",
    "tiktok": "🎵 TikTok",
}


def _esc(val: str | None) -> str:
    return html.escape(str(val or ""))


def _trim_memory_caches():
    """Xotira keshlari cheksiz o'sib ketmasligi uchun."""
    if len(track_search_cache) > 2000:
        for k in list(track_search_cache.keys())[:500]:
            track_search_cache.pop(k, None)
    if len(search_results_cache) > 1000:
        for k in list(search_results_cache.keys())[:300]:
            search_results_cache.pop(k, None)


# ─── FOYDALANUVCHINI TEKSHIRISH VA BAZAGA YOZISH ───────────────

async def pre_process_user(message: types.Message, ref_code: str | None = None) -> bool:
    """
    Bitta bazaviy so'rovda foydalanuvchini yangilaydi, ban va texnik rejimni tekshiradi.
    """
    user = message.from_user
    if not user:
        return False

    db_user = await db.add_or_update_user(user.id, user.username, user.first_name, ref_code=ref_code)

    if is_admin(user.id, user.username):
        return True

    if db_user and db_user.get("is_banned"):
        reason = db_user.get("ban_reason")
        reason_line = f"\n📌 Sabab: <i>{_esc(reason)}</i>" if reason else ""
        await message.reply(
            f"🚫 <b>Siz botdan foydalanishdan cheklangansiz (Ban).</b>{reason_line}\n"
            "Murojaat uchun adminga yozing.",
            parse_mode=ParseMode.HTML,
        )
        return False

    if db.get_bool_setting("maintenance_mode", False):
        maint_text = db.get_setting(
            "maintenance_text",
            "🛠 <b>Botda texnik ishlar olib borilmoqda.</b>\n\nBirozdan so'ng qayta urinib ko'ring!",
        )
        await message.reply(maint_text, parse_mode=ParseMode.HTML)
        return False

    return True


async def pre_process_callback(callback: CallbackQuery) -> bool:
    """Callback so'rovlari uchun ban va texnik rejim tekshiruvi."""
    user = callback.from_user
    if not user:
        return False
    if is_admin(user.id, user.username):
        return True

    db_user = await db.get_user(user.id)
    if db_user and db_user.get("is_banned"):
        await callback.answer("🚫 Siz botdan foydalanishdan cheklangansiz.", show_alert=True)
        return False

    if db.get_bool_setting("maintenance_mode", False):
        await callback.answer("🛠 Botda texnik ishlar olib borilmoqda. Birozdan so'ng urinib ko'ring!", show_alert=True)
        return False

    return True


async def _check_rate_and_reply(message: types.Message) -> bool:
    user = message.from_user
    if not user or is_admin(user.id, user.username):
        return True
    allowed, wait_sec = check_rate_limit(user.id)
    if not allowed:
        await message.reply(
            f"⏳ <b>Juda tez so'rov yuboryapsiz!</b>\n"
            f"Iltimos, <b>{wait_sec}</b> soniyadan so'ng qayta urinib ko'ring.",
            parse_mode=ParseMode.HTML,
        )
        return False
    return True


# ═══════════════════════════════════════════════════════════════
#                         BUYRUQLAR
# ═══════════════════════════════════════════════════════════════

@main_router.message(Command("start"))
async def cmd_start(message: types.Message):
    ref_code = None
    if message.text:
        parts = message.text.split(maxsplit=1)
        if len(parts) == 2 and parts[1].strip():
            raw_ref = parts[1].strip()
            ref_code = raw_ref[4:] if raw_ref.startswith("ref_") else raw_ref
            ref_code = ref_code[:32]

    if not await pre_process_user(message, ref_code=ref_code):
        return

    admin_hint = ""
    if is_admin(message.from_user.id, message.from_user.username):
        admin_hint = "\n\n👑 <b>Siz adminsiz!</b> Boshqaruv paneli: /admin"

    custom_welcome = db.get_setting("welcome_text", "").strip()
    if custom_welcome:
        text = custom_welcome.replace("{name}", _esc(message.from_user.first_name or "Do'st")) + admin_hint
    else:
        text = (
            "🎵 <b>Tarona Izla</b> botiga xush kelibsiz!\n\n"
            "📌 <b>Bot nimalar qila oladi?</b>\n\n"
            "1️⃣  <b>Ijtimoiy tarmoqlardan video link yuboring:</b>\n"
            "  • 📸 Instagram Reels\n"
            "  • 🎵 TikTok\n"
            "  • ▶️ YouTube / Shorts\n\n"
            "2️⃣  <b>Qo'shiq nomini yozib yuboring:</b>\n"
            "  • 🔍 Masalan: <code>Munisa Rizayeva</code> yoki <code>Konsta O'ylamading</code>\n\n"
            "3️⃣  <b>Yoki to'g'ridan-to'g'ri yuboring:</b>\n"
            "  • 🎤 Ovozli xabar (Voice) yozib yuboring\n"
            "  • 🎬 Video yoki audio fayl uzating\n\n"
            "Men sizga videoni va musiqani <b>MP3 formatda</b> taqdim etaman!\n\n"
            f"👇 <b>Link, qo'shiq nomi yoki audio yuboring:</b>{admin_hint}"
        )

    await message.answer(text, parse_mode=ParseMode.HTML)


@main_router.message(Command("help"))
async def cmd_help(message: types.Message):
    if not await pre_process_user(message):
        return

    await message.answer(
        "❓ <b>Qo'llanma</b>\n\n"
        "• Instagram Reels, TikTok yoki YouTube'dan link nusxalang va shu yerga yuboring.\n"
        "• Bot videoni yuklaydi va undagi musiqani MP3 qilib beradi.\n"
        "• Agar to'liq studiya qo'shig'i kerak bo'lsa, audio tagidagi 'To'liq musiqani yuklash' tugmasini bosing.\n"
        "• Shuningdek, ovozli xabar (voice) yuborsangiz ham undagi qo'shiqni aniqlab beradi.\n\n"
        "🔧 /start — Qayta ishga tushirish\n"
        "🔧 /help — Qo'llanma",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════════
#             MAJBURIY OBUNA CALLBACK TEKSHIRUVI
# ═══════════════════════════════════════════════════════════════

@main_router.callback_query(F.data == "check_subscription")
async def cb_check_subscription(callback: CallbackQuery):
    user_id = callback.from_user.id
    is_subbed, _ = await check_user_subscription(bot, user_id, from_check_button=True)

    if is_subbed:
        await callback.answer("🎉 A'zoligingiz tasdiqlandi! Rahmat.", show_alert=True)
        try:
            await callback.message.delete()
        except Exception:
            pass
        await callback.message.answer(
            "✅ <b>Homiy kanallarga a'zolik tasdiqlandi!</b>\n\n"
            "Endi bemalol video link, qo'shiq nomi yoki ovozli xabar yuborishingiz mumkin.",
            parse_mode=ParseMode.HTML,
        )
    else:
        await callback.answer(
            "❌ Hali hamma homiy kanallarga a'zo bo'lmadingiz!\nIltimos, a'zo bo'lib qayta bosing.",
            show_alert=True,
        )


# ═══════════════════════════════════════════════════════════════
#           TURBO PARALLEL VIDEO + AUDIO + SHAZAM KONVEYERI
# ═══════════════════════════════════════════════════════════════

async def _audio_and_shazam_from_stream(
    media: ExtractedMedia,
    request_dir: Path,
) -> tuple[Path | None, Path | None, MusicResult]:
    """
    Kichik DASH audio oqimini (65-200KB) ~0.3s da yuklaydi,
    MP3 ga o'tkazadi (~0.12s — Linux HE-AAC muammosini ham to'liq hal qiladi) va Shazam qiladi!
    Returns: (raw_audio_path, mp3_path, music_result)
    """
    raw_audio = await Downloader.download_audio_stream(media, request_dir)
    if not raw_audio or not raw_audio.exists():
        return None, None, MusicResult(found=False)

    mp3_path = request_dir / "audio.mp3"
    mp3_ok = await Downloader.extract_audio_from_video(raw_audio, mp3_path)
    if not mp3_ok or not mp3_path.exists():
        return raw_audio, None, MusicResult(found=False)

    target = mp3_path
    if media.duration > 35:
        snip = request_dir / "snip.mp3"
        if await Downloader.create_snippet(mp3_path, snip, duration=12):
            target = snip

    music = await recognizer.recognize(target)
    return raw_audio, mp3_path, music


async def _audio_and_shazam_from_video(
    video_path: Path,
    request_dir: Path,
) -> tuple[Path | None, MusicResult]:
    """Lokal video fayldan MP3 ajratish va Shazam orqali musiqani aniqlash."""
    mp3_path = request_dir / "audio.mp3"
    ok = await Downloader.extract_audio_from_video(video_path, mp3_path)
    if not ok:
        return None, MusicResult(found=False)

    snippet_path = request_dir / "snippet.mp3"
    target = snippet_path if await Downloader.create_snippet(mp3_path, snippet_path, duration=10) else mp3_path
    music = await recognizer.recognize(target)
    return mp3_path, music


@main_router.message(F.text.regexp(r"https?://"))
async def handle_link(message: types.Message):
    if not await pre_process_user(message):
        return

    user_id = message.from_user.id
    admin_user = is_admin(user_id, message.from_user.username)

    if not admin_user:
        is_subbed, missing = await check_user_subscription(bot, user_id)
        if not is_subbed:
            await message.reply(
                "⚠️ <b>Botdan foydalanish uchun quyidagi homiy kanallarga a'zo bo'ling:</b>\n\n"
                "A'zo bo'lgach, <b>'✅ A'zo bo'ldim'</b> tugmasini bosing:",
                parse_mode=ParseMode.HTML,
                reply_markup=get_subscription_keyboard(missing),
            )
            return

    url = Downloader.extract_url(message.text)
    if not url:
        await message.reply("❌ Havola topilmadi. To'g'ri havola yuboring.")
        return

    platform = Downloader.detect_platform(url)
    if not platform:
        await message.reply(
            "❌ Bu platforma qo'llab-quvvatlanmaydi.\n\n"
            "Qo'llab-quvvatlanadi:\n"
            "  • 📸 Instagram Reels\n"
            "  • 🎵 TikTok\n"
            "  • ▶️ YouTube / Shorts",
            parse_mode=ParseMode.HTML,
        )
        return

    if not admin_user and not db.get_bool_setting(f"enable_{platform}", True):
        pname = PLATFORM_NAME.get(platform, platform)
        await message.reply(
            f"⚠️ <b>{pname}</b> bo'limi vaqtincha o'chirilgan.\nBirozdan so'ng qayta urinib ko'ring.",
            parse_mode=ParseMode.HTML,
        )
        return

    if not await _check_rate_and_reply(message):
        return

    if is_download_queue_busy():
        await message.reply("⏳ Hozir ko'p so'rovlar qayta ishlanmoqda. Biroz kuting...")

    async with acquire_download_slot(user_id, platform, url):
        await _process_video_link(message, url, platform)


async def _process_video_link(message: types.Message, url: str, platform: str):
    """
    Turbo parallel video + MP3 + Shazam konveyeri:
    - Keshda bor bo'lsa: ~0.1 soniyada uzatadi.
    - Instagram: Telegram Direct CDN + kichik DASH audio + Shazam BIR VAQTDA (~2.5-3.5s!).
    - YouTube/TikTok: 6x parallel yuklash + audio/Shazam BIR VAQTDA!
    - 'To'liq musiqani yuklash' tugmasi FAQAT bitta joyda — audio (MP3) tagida chiqadi!
    """
    if platform == "youtube":
        parsed = urlparse(url)
        vid = parse_qs(parsed.query).get("v", [None])[0]
        clean_url = f"https://www.youtube.com/watch?v={vid}" if vid else url.split("?")[0].rstrip("/")
    else:
        clean_url = url.split("?")[0].rstrip("/")

    cache_enabled = db.get_bool_setting("cache_enabled", True)

    # ── 0. TEZKOR KESH (1 ta so'rovda video + audio, ~0.1s!) ──
    if cache_enabled:
        cached_video, cached_audio = await db.get_cached_pair(clean_url)
        if cached_video and cached_audio:
            try:
                keyboard = None
                c_title = cached_audio.get("title") or ""
                c_perf = cached_audio.get("performer") or ""
                if c_title and c_perf and c_perf != "@taronaizlauzbot":
                    _trim_memory_caches()
                    token = uuid.uuid4().hex[:10]
                    track_search_cache[token] = f"{c_perf} - {c_title}"
                    keyboard = InlineKeyboardMarkup(inline_keyboard=[[
                        InlineKeyboardButton(
                            text="🎧 To'liq musiqani yuklash (Full MP3)",
                            callback_data=f"dl_full:{token}",
                        )
                    ]])

                music_line = (
                    f"\n\n🎵 <b>Musiqa:</b> {_esc(c_perf)} — {_esc(c_title)}"
                    if (c_title and c_perf and c_perf != "@taronaizlauzbot")
                    else ""
                )
                v_caption = format_caption_with_footer(
                    f"🎬 <b>Video tayyor!</b>{music_line}\n\n🤖 @taronaizlauzbot"
                )
                # Video tagida tugma YO'Q — bitta tugma faqat audio tagida!
                await message.reply_video(
                    video=cached_video["file_id"],
                    caption=v_caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )

                a_caption = format_caption_with_footer(
                    f"🎵 <b>{_esc(c_perf)} — {_esc(c_title)}</b>\n\n🤖 @taronaizlauzbot orqali yuklab olindi"
                )
                await message.reply_audio(
                    audio=cached_audio["file_id"],
                    title=c_title,
                    performer=c_perf,
                    caption=a_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=keyboard,
                )

                await db.log_download(
                    message.from_user.id, platform, "video", from_cache=True, detail=clean_url
                )
                return
            except Exception:
                pass

    pname = PLATFORM_NAME.get(platform, platform)
    status_msg = await message.reply(f"⏳ {pname} dan yuklanmoqda...")

    request_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:8]
    request_dir.mkdir(exist_ok=True)

    try:
        # 1. In-process YoutubeDL orqali oqimlarni olish (~1.1-1.5s)
        media = await Downloader.extract_media_info(clean_url, platform)
        if not media or (not media.progressive_url and not media.video_only_url):
            await report_error(bot, f"extract_{platform}", f"Oqim topilmadi: {clean_url}", message.from_user.id, critical=False)
            await status_msg.edit_text(
                "❌ Videoni yuklab bo'lmadi.\n"
                "Profil yopiq (private) bo'lishi yoki havola o'chirilgan bo'lishi mumkin."
            )
            return

        initial_video_caption = format_caption_with_footer(
            "🎬 <b>Video tayyor!</b>\n\n🤖 @taronaizlauzbot"
        )

        sent_video: types.Message | None = None
        mp3_path: Path | None = None
        music = MusicResult(found=False)

        # 2A. ENG TEZ YO'L (Instagram Direct CDN + kichik DASH audio PARALLEL!)
        if media.telegram_direct_eligible and media.progressive_url:
            audio_task = (
                asyncio.create_task(_audio_and_shazam_from_stream(media, request_dir))
                if media.audio_only_url
                else None
            )

            async def _send_video_direct_or_fallback() -> tuple[types.Message | None, Path | None]:
                try:
                    msg = await message.reply_video(
                        video=media.progressive_url,
                        caption=initial_video_caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                    )
                    return msg, None
                except Exception:
                    # Telegram URL ni qabul qilmasa — 6x parallel lokal yuklash zaxirasi
                    local_v = await Downloader.download_video_from_media(media, request_dir)
                    if not local_v or not local_v.exists():
                        return None, None
                    msg = await message.reply_video(
                        video=FSInputFile(str(local_v)),
                        caption=initial_video_caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                    )
                    return msg, local_v

            sent_video, fallback_video_path = await _send_video_direct_or_fallback()

            if not sent_video:
                if audio_task:
                    audio_task.cancel()
                await report_error(bot, f"download_{platform}", f"Video yuborilmadi: {clean_url}", message.from_user.id, critical=False)
                await status_msg.edit_text(
                    "❌ Videoni yuklab bo'lmadi.\n"
                    "Profil yopiq (private) bo'lishi yoki havola o'chirilgan bo'lishi mumkin."
                )
                return

            try:
                await status_msg.delete()
            except Exception:
                pass

            if audio_task:
                _, mp3_path, music = await audio_task
            if mp3_path is None:
                # Agar DASH audio bo'lmagan bo'lsa videodan ajratamiz
                v_for_audio = fallback_video_path or await Downloader.download_video_from_media(media, request_dir)
                if v_for_audio and v_for_audio.exists():
                    mp3_path, music = await _audio_and_shazam_from_video(v_for_audio, request_dir)

        # 2B. LOKAL PARALLEL YO'L (YouTube Shorts / TikTok / DASH-only)
        else:
            if media.audio_only_url:
                # Video va kichik audio+Shazam BIR VAQTDA bajariladi!
                audio_task = asyncio.create_task(_audio_and_shazam_from_stream(media, request_dir))
                video_path = await Downloader.download_video_from_media(media, request_dir)

                if not video_path or not video_path.exists():
                    audio_task.cancel()
                    await report_error(bot, f"download_{platform}", f"Yuklab bo'lmadi: {clean_url}", message.from_user.id, critical=False)
                    await status_msg.edit_text(
                        "❌ Videoni yuklab bo'lmadi.\n"
                        "Profil yopiq (private) bo'lishi yoki havola o'chirilgan bo'lishi mumkin."
                    )
                    return

                # Videoni yuklash tugashi bilan darhol Telegram'ga uzatamiz (audio_task fonda tugaydi!)
                sent_video = await message.reply_video(
                    video=FSInputFile(str(video_path)),
                    caption=initial_video_caption,
                    parse_mode=ParseMode.HTML,
                    supports_streaming=True,
                )
                try:
                    await status_msg.delete()
                except Exception:
                    pass

                _, mp3_path, music = await audio_task
                if mp3_path is None:
                    mp3_path, music = await _audio_and_shazam_from_video(video_path, request_dir)
            else:
                # Faqat progressiv oqim bor (masalan ayrim TikTok) -> video yuklab, upload + MP3/Shazam PARALLEL!
                video_path = await Downloader.download_video_from_media(media, request_dir)
                if not video_path or not video_path.exists():
                    await report_error(bot, f"download_{platform}", f"Yuklab bo'lmadi: {clean_url}", message.from_user.id, critical=False)
                    await status_msg.edit_text(
                        "❌ Videoni yuklab bo'lmadi.\n"
                        "Profil yopiq (private) bo'lishi yoki havola o'chirilgan bo'lishi mumkin."
                    )
                    return

                sent_video, (mp3_path, music) = await asyncio.gather(
                    message.reply_video(
                        video=FSInputFile(str(video_path)),
                        caption=initial_video_caption,
                        parse_mode=ParseMode.HTML,
                        supports_streaming=True,
                    ),
                    _audio_and_shazam_from_video(video_path, request_dir),
                )
                try:
                    await status_msg.delete()
                except Exception:
                    pass

        # 3. Tugmani tayyorlash (BITTA TUGMA — faqat audio ostida!)
        keyboard = None
        if music.found:
            _trim_memory_caches()
            token = uuid.uuid4().hex[:10]
            track_search_cache[token] = f"{music.artist} - {music.title}"
            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🎧 To'liq musiqani yuklash (Full MP3)",
                        callback_data=f"dl_full:{token}",
                    )
                ]]
            )

        # 4. MP3 yuborish + video sarlavhasini yangilash (PARALLEL!)
        has_mp3 = bool(mp3_path and mp3_path.exists() and mp3_path.stat().st_size > 0)

        async def _send_audio():
            if not has_mp3:
                return None
            performer = music.artist if music.found else "@taronaizlauzbot"
            title = music.title if music.found else "Videodagi audio"
            audio_caption = format_caption_with_footer(
                f"🎵 <b>{_esc(performer)} — {_esc(title)}</b>\n\n🤖 @taronaizlauzbot orqali yuklab olindi"
            )
            return await message.reply_audio(
                audio=FSInputFile(str(mp3_path)),
                title=title,
                performer=performer,
                caption=audio_caption,
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

        async def _update_video_caption():
            if not (music.found and sent_video):
                return
            try:
                base_v = (
                    f"🎬 <b>Video tayyor!</b>\n\n"
                    f"🎵 <b>Musiqa:</b> {_esc(music.artist)} — {_esc(music.title)}\n"
                )
                if music.album:
                    base_v += f"💿 <b>Album:</b> {_esc(music.album)}\n"
                base_v += "\n🤖 @taronaizlauzbot"
                # Video ostida takroriy tugma BO'LMAYDI (faqat MP3 chiqmagan holatdagina zaxira)
                await sent_video.edit_caption(
                    caption=format_caption_with_footer(base_v),
                    parse_mode=ParseMode.HTML,
                    reply_markup=None if has_mp3 else keyboard,
                )
            except Exception:
                pass

        sent_audio, _ = await asyncio.gather(_send_audio(), _update_video_caption())

        # 5. Keshga saqlash (faqat musiqa muvaffaqiyatli aniqlangan bo'lsa saqlaymiz!) va jurnalga yozish
        if cache_enabled and music.found:
            if sent_video and sent_video.video:
                await db.set_cached_media(clean_url, sent_video.video.file_id, "video")
            if sent_audio and sent_audio.audio:
                await db.set_cached_media(clean_url, sent_audio.audio.file_id, "audio", music.title, music.artist)

        await db.log_download(
            message.from_user.id, platform, "video", from_cache=False, detail=clean_url
        )

    except Exception as e:
        await report_error(bot, f"video_{platform}", str(e), message.from_user.id, critical=True)
        try:
            await status_msg.edit_text(
                f"❌ Xatolik yuz berdi:\n<code>{_esc(str(e)[:200])}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
#               OVOZLI XABARLAR VA TELEGRAM VIDEOLARI
# ═══════════════════════════════════════════════════════════════

@main_router.message(F.voice | F.audio | F.video)
async def handle_media_file(message: types.Message):
    if not await pre_process_user(message):
        return

    user_id = message.from_user.id
    admin_user = is_admin(user_id, message.from_user.username)

    if not admin_user:
        is_subbed, missing = await check_user_subscription(bot, user_id)
        if not is_subbed:
            await message.reply(
                "⚠️ <b>Botdan foydalanish uchun quyidagi homiy kanallarga a'zo bo'ling:</b>\n\n"
                "A'zo bo'lgach, <b>'✅ A'zo bo'ldim'</b> tugmasini bosing:",
                parse_mode=ParseMode.HTML,
                reply_markup=get_subscription_keyboard(missing),
            )
            return

        if not db.get_bool_setting("enable_voice", True):
            await message.reply("⚠️ <b>Ovozli qidiruv (Shazam)</b> bo'limi vaqtincha o'chirilgan.", parse_mode=ParseMode.HTML)
            return

    if not await _check_rate_and_reply(message):
        return

    status_msg = await message.reply("⏳ Musiqa tahlil qilinmoqda...")
    request_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:8]
    request_dir.mkdir(exist_ok=True)

    try:
        async with acquire_download_slot(user_id, "voice", "media"):
            file_obj = message.voice or message.audio or message.video
            telegram_file = await bot.get_file(file_obj.file_id)

            input_path = request_dir / f"input_{uuid.uuid4().hex[:6]}"
            await bot.download_file(telegram_file.file_path, destination=input_path)

            mp3_path = request_dir / "converted.mp3"
            await Downloader.convert_to_mp3(input_path, mp3_path)

            if not mp3_path.exists():
                await status_msg.edit_text("❌ Faylni qayta ishlab bo'lmadi.")
                return

            music = await recognizer.recognize(mp3_path)

            if not music.found:
                await status_msg.edit_text(
                    "❌ <b>Musiqa topilmadi</b>\n\n"
                    "Qo'shiq juda sokin, fon shovqini baland yoki bazada mavjud emas.",
                    parse_mode=ParseMode.HTML,
                )
                return

            _trim_memory_caches()
            search_query = f"{music.artist} - {music.title}"
            token = uuid.uuid4().hex[:10]
            track_search_cache[token] = search_query

            keyboard = InlineKeyboardMarkup(
                inline_keyboard=[[
                    InlineKeyboardButton(
                        text="🎧 To'liq musiqani yuklash (Full MP3)",
                        callback_data=f"dl_full:{token}",
                    )
                ]]
            )

            base_caption = (
                f"🎵 <b>Musiqa aniqlandi:</b>\n"
                f"🎤 Ijrochi: <b>{_esc(music.artist)}</b>\n"
                f"🎶 Qo'shiq: <b>{_esc(music.title)}</b>\n"
            )
            if music.album:
                base_caption += f"💿 Album: {_esc(music.album)}\n"
            if music.year:
                base_caption += f"📅 Yil: {_esc(music.year)}\n"
            base_caption += "\n🤖 @taronaizlauzbot"

            await message.reply(
                format_caption_with_footer(base_caption),
                parse_mode=ParseMode.HTML,
                reply_markup=keyboard,
            )

            await db.log_download(user_id, "voice", "audio", from_cache=False, detail=search_query)

            try:
                await status_msg.delete()
            except Exception:
                pass

    except Exception as e:
        await report_error(bot, "voice_recognize", str(e), user_id, critical=True)
        try:
            await status_msg.edit_text(f"❌ Xatolik: {_esc(str(e)[:150])}")
        except Exception:
            pass
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
#             TO'LIQ MP3 YUKLASH CALLBACK HANDLERI
# ═══════════════════════════════════════════════════════════════

@main_router.callback_query(F.data.startswith("dl_full:"))
async def handle_download_full(callback: CallbackQuery):
    if not await pre_process_callback(callback):
        return

    token = callback.data.split(":", 1)[1]
    query = track_search_cache.get(token)

    if not query:
        await callback.answer(
            "⚠️ So'rov eskirgan. Iltimos, linkni qayta yuboring.", show_alert=True
        )
        return

    cache_enabled = db.get_bool_setting("cache_enabled", True)

    if cache_enabled:
        cached_full = await db.get_cached_media(query, "full_mp3")
        if cached_full:
            try:
                parts = query.split(" - ", 1)
                artist = parts[0] if len(parts) > 1 else "@taronaizlauzbot"
                title = parts[1] if len(parts) > 1 else query
                caption = format_caption_with_footer(
                    f"🎧 <b>{_esc(artist)} — {_esc(title)}</b> (To'liq versiya)\n\n🤖 @taronaizlauzbot"
                )
                await callback.answer("⚡ Tayyor!")
                await callback.message.reply_audio(
                    audio=cached_full["file_id"],
                    title=title,
                    performer=artist,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                await db.log_download(
                    callback.from_user.id, "youtube", "full_mp3", from_cache=True, detail=query
                )
                return
            except Exception:
                pass

    await callback.answer("⏳ To'liq musiqa yuklanmoqda...")

    status_msg = await callback.message.reply(
        f"⏳ <b>{_esc(query)}</b> — to'liq trek yuklanmoqda...",
        parse_mode=ParseMode.HTML,
    )

    request_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:8]
    request_dir.mkdir(exist_ok=True)

    try:
        async with acquire_download_slot(callback.from_user.id, "youtube", query):
            full_track_path = await Downloader.download_full_track(query, request_dir)

        if full_track_path and full_track_path.exists():
            parts = query.split(" - ", 1)
            artist = parts[0] if len(parts) > 1 else "@taronaizlauzbot"
            title = parts[1] if len(parts) > 1 else query

            caption = format_caption_with_footer(
                f"🎧 <b>{_esc(artist)} — {_esc(title)}</b> (To'liq versiya)\n\n🤖 @taronaizlauzbot"
            )

            sent_audio = await callback.message.reply_audio(
                audio=FSInputFile(str(full_track_path)),
                title=title,
                performer=artist,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

            if cache_enabled and sent_audio and sent_audio.audio:
                await db.set_cached_media(query, sent_audio.audio.file_id, "full_mp3", title, artist)

            await db.log_download(
                callback.from_user.id, "youtube", "full_mp3", from_cache=False, detail=query
            )

            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text(
                f"❌ <b>{_esc(query)}</b> ning to'liq versiyasi topilmadi.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        await report_error(bot, "full_mp3", str(e), callback.from_user.id, critical=True)
        try:
            await status_msg.edit_text(f"❌ Xatolik yuz berdi: {_esc(str(e)[:150])}")
        except Exception:
            pass
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
#             QO'SHIQ NOMI BO'YICHA QIDIRISH (SEARCH)
# ═══════════════════════════════════════════════════════════════

NUMBER_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


@main_router.message(F.text)
async def handle_song_search(message: types.Message):
    if not await pre_process_user(message):
        return

    query = message.text.strip()
    if len(query) < 2:
        await message.reply("👋 Menga qo'shiq nomi, video link yoki ovozli xabar yuboring!")
        return

    user_id = message.from_user.id
    admin_user = is_admin(user_id, message.from_user.username)

    if not admin_user:
        is_subbed, missing = await check_user_subscription(bot, user_id)
        if not is_subbed:
            await message.reply(
                "⚠️ <b>Qo'shiqni yuklab olish uchun quyidagi homiy kanallarga a'zo bo'ling:</b>\n\n"
                "A'zo bo'lgach, <b>'✅ A'zo bo'ldim'</b> tugmasini bosing:",
                parse_mode=ParseMode.HTML,
                reply_markup=get_subscription_keyboard(missing),
            )
            return

        if not db.get_bool_setting("enable_search", True):
            await message.reply("⚠️ <b>Qo'shiq qidiruvi</b> bo'limi vaqtincha o'chirilgan.", parse_mode=ParseMode.HTML)
            return

    if not await _check_rate_and_reply(message):
        return

    status_msg = await message.reply(
        f"🔍 <b>\"{_esc(query)}\"</b> qidirilmoqda...",
        parse_mode=ParseMode.HTML,
    )

    try:
        tracks = await Downloader.search_tracks(query, limit=5)
        if not tracks:
            await status_msg.edit_text(
                f"❌ <b>\"{_esc(query)}\"</b> bo'yicha hech qanday tarona topilmadi.\n\n"
                "Iltimos, boshqacha so'z yoki qo'shiqchi nomi bilan qidirib ko'ring.",
                parse_mode=ParseMode.HTML,
            )
            return

        _trim_memory_caches()
        session_id = uuid.uuid4().hex[:8]
        search_results_cache[session_id] = tracks

        text = f"🔍 <b>\"{_esc(query)}\"</b> bo'yicha topilgan taronalar:\n\n"
        button_row = []

        for i, tr in enumerate(tracks):
            emoji = NUMBER_EMOJIS[i] if i < len(NUMBER_EMOJIS) else f"{i+1}."
            duration_text = f" ⏳ <i>{_esc(tr['duration'])}</i>" if tr.get("duration") else ""
            text += f"{emoji} <b>{_esc(tr['title'])}</b>{duration_text}\n\n"

            button_row.append(
                InlineKeyboardButton(
                    text=emoji,
                    callback_data=f"pick_track:{session_id}:{i}",
                )
            )

        text += "👇 <i>Yuklab olmoqchi bo'lgan taronangiz raqamini tanlang:</i>"
        keyboard = InlineKeyboardMarkup(inline_keyboard=[button_row])

        await status_msg.edit_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=keyboard,
        )

    except Exception as e:
        await report_error(bot, "song_search", str(e), user_id, critical=False)
        try:
            await status_msg.edit_text(f"❌ Qidiruvda xatolik yuz berdi: {_esc(str(e)[:150])}")
        except Exception:
            pass


@main_router.callback_query(F.data.startswith("pick_track:"))
async def handle_pick_track(callback: CallbackQuery):
    if not await pre_process_callback(callback):
        return

    parts = callback.data.split(":")
    session_id = parts[1]
    track_idx = int(parts[2])

    tracks = search_results_cache.get(session_id)
    if not tracks or track_idx >= len(tracks):
        await callback.answer(
            "⚠️ Qidiruv natijasi eskirgan. Qaytadan qo'shiq nomini yozing.",
            show_alert=True,
        )
        return

    track = tracks[track_idx]
    cache_enabled = db.get_bool_setting("cache_enabled", True)

    if cache_enabled:
        cached_track = await db.get_cached_media(track["id"], "search_mp3")
        if cached_track:
            try:
                full_title = track["title"]
                title_parts = full_title.split(" - ", 1)
                artist = title_parts[0].strip() if len(title_parts) > 1 else "@taronaizlauzbot"
                title = title_parts[1].strip() if len(title_parts) > 1 else full_title
                caption = format_caption_with_footer(
                    f"🎵 <b>{_esc(artist)} — {_esc(title)}</b>\n\n🤖 @taronaizlauzbot orqali yuklab olindi"
                )
                await callback.answer("⚡ Tayyor!")
                await callback.message.reply_audio(
                    audio=cached_track["file_id"],
                    title=title,
                    performer=artist,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                )
                await db.log_download(
                    callback.from_user.id, "search", "mp3", from_cache=True, detail=full_title
                )
                return
            except Exception:
                pass

    await callback.answer(f"⏳ {track['title'][:30]} yuklanmoqda...")

    status_msg = await callback.message.reply(
        f"⏳ <b>{_esc(track['title'])}</b> — MP3 yuklanmoqda...",
        parse_mode=ParseMode.HTML,
    )

    request_dir = DOWNLOAD_DIR / uuid.uuid4().hex[:8]
    request_dir.mkdir(exist_ok=True)

    try:
        async with acquire_download_slot(callback.from_user.id, "search", track["title"]):
            mp3_path = await Downloader.download_track_by_id(track["id"], request_dir)

        if mp3_path and mp3_path.exists():
            full_title = track["title"]
            title_parts = full_title.split(" - ", 1)
            artist = title_parts[0].strip() if len(title_parts) > 1 else "@taronaizlauzbot"
            title = title_parts[1].strip() if len(title_parts) > 1 else full_title

            caption = format_caption_with_footer(
                f"🎵 <b>{_esc(artist)} — {_esc(title)}</b>\n\n🤖 @taronaizlauzbot orqali yuklab olindi"
            )

            sent_audio = await callback.message.reply_audio(
                audio=FSInputFile(str(mp3_path)),
                title=title,
                performer=artist,
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

            if cache_enabled and sent_audio and sent_audio.audio:
                await db.set_cached_media(track["id"], sent_audio.audio.file_id, "search_mp3", title, artist)

            await db.log_download(
                callback.from_user.id, "search", "mp3", from_cache=False, detail=full_title
            )

            try:
                await status_msg.delete()
            except Exception:
                pass
        else:
            await status_msg.edit_text(
                f"❌ <b>{_esc(track['title'])}</b> ni yuklab bo'lmadi. Boshqa variantni tanlab ko'ring.",
                parse_mode=ParseMode.HTML,
            )
    except Exception as e:
        await report_error(bot, "pick_track", str(e), callback.from_user.id, critical=True)
        try:
            await status_msg.edit_text(f"❌ Xatolik yuz berdi: {_esc(str(e)[:150])}")
        except Exception:
            pass
    finally:
        shutil.rmtree(request_dir, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════
#                           MAIN
# ═══════════════════════════════════════════════════════════════

def _clean_downloads_dir():
    """Ishga tushishda eski vaqtinchalik fayllarni avtomatik tozalaydi."""
    DOWNLOAD_DIR.mkdir(exist_ok=True)
    for item in DOWNLOAD_DIR.iterdir():
        try:
            if item.is_file():
                item.unlink(missing_ok=True)
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
        except Exception:
            pass


async def _run_webhook_server():
    """Alwaysdata (Web -> Sites -> User program) uchun 24/7 aiohttp Webhook serveri."""
    from aiohttp import web
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

    webhook_url = f"{WEBHOOK_HOST}{WEBHOOK_PATH}"

    await db.init_db()
    await Downloader.warmup()
    _clean_downloads_dir()

    app = web.Application()
    app.router.add_get("/", lambda _req: web.Response(text="Tarona Izla Bot OK"))
    app.router.add_get("/health", lambda _req: web.Response(text="OK"))

    handler = SimpleRequestHandler(
        dispatcher=dp,
        bot=bot,
        secret_token=WEBHOOK_SECRET or None,
    )
    handler.register(app, path=WEBHOOK_PATH)
    setup_application(app, dp, bot=bot)

    runner = web.AppRunner(app)
    await runner.setup()

    clean_host = WEB_HOST.strip("[]") or "::"
    site = web.TCPSite(runner, host=clean_host, port=WEB_PORT)
    await site.start()

    print("=" * 54)
    print(f"[*] HTTP server tinglanmoqda: [{clean_host}]:{WEB_PORT} (LOW_RAM={LOW_RAM_MODE})")

    await bot.set_webhook(
        url=webhook_url,
        secret_token=WEBHOOK_SECRET or None,
        drop_pending_updates=False,
    )
    print(f"[*] Telegram Webhook ulandi: {webhook_url}")
    print("=" * 54)

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
        await Downloader.close()
        await db.close()


async def main():
    if not BOT_TOKEN or BOT_TOKEN == "your_bot_token_here":
        print("[!] BOT_TOKEN topilmadi!")
        return

    if WEBHOOK_HOST:
        await _run_webhook_server()
        return

    # Polling rejimi (lokal yoki VPS)
    await db.init_db()
    await Downloader.warmup()
    _clean_downloads_dir()
    await bot.delete_webhook(drop_pending_updates=False)

    print("=" * 52)
    print(f"[*] Tarona Izla TURBO + Pro Admin Panel 2.0 ishga tushdi!")
    print(f"[*] Asosiy Admin ID: {PRIMARY_ADMIN_ID} | LOW_RAM={LOW_RAM_MODE}")
    print("=" * 52)

    try:
        await dp.start_polling(bot)
    finally:
        await Downloader.close()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
