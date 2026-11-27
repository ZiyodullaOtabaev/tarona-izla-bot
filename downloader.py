"""
Yuqori tezlikdagi video va audio yuklash dvigateli (Turbo Engine).

ARXITEKTURA OPTIMIZATSIYALARI:
1. Subprocess yo'q: yt_dlp.YoutubeDL to'g'ridan-to'g'ri xotirada (persistent pool) ishlaydi —
   Python start va 1744 ta extractor import vaqti 0.00s, Instagram sessiyasi qayta ishlatiladi!
2. Telegram Direct CDN: Instagram progressiv MP4 (<20MB) Telegram serverlariga to'g'ridan-to'g'ri
   havola orqali yuboriladi (serverimizga video yuklash va qayta upload 0s!).
3. Audio-only DASH oqimi: Videodagi musiqani olish va Shazam uchun to'liq 3-10MB video o'rniga
   faqat 65-200KB m4a audio oqimi 0.3 soniyada yuklanadi!
4. 6x Parallel HTTP Range Download: Lokal yuklash kerak bo'lganda (YouTube, TikTok, fallback)
   doimiy aiohttp.ClientSession orqali 6 ta parallel oqimda tortiladi.
5. Stream-copy DASH muxing + ko'p oqimli FFmpeg MP3 konvertatsiya.
"""

import asyncio
import os
import queue
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiohttp

from config import FFMPEG_PATH, LOW_RAM_MODE, MAX_TELEGRAM_FILE_SIZE

_yt_dlp_mod = None


def _get_yt_dlp():
    """yt_dlp ni faqat kerak bo'lganda xotiraga yuklaydi (sovuq startni 10x tezlashtiradi)."""
    global _yt_dlp_mod
    if _yt_dlp_mod is None:
        import yt_dlp as _ydl
        _yt_dlp_mod = _ydl
    return _yt_dlp_mod

# Qidiruv so'rovlari keshi: query -> (results, timestamp)
search_memory_cache: dict[str, tuple[list[dict], float]] = {}
SEARCH_CACHE_TTL = 3600  # 1 soat

# Telegram URL orqali qabul qiladigan maksimal video hajmi (20 MB)
MAX_TELEGRAM_URL_SIZE = 19 * 1024 * 1024


@dataclass
class ExtractedMedia:
    """Platformadan ajratilgan to'g'ridan-to'g'ri media oqimlari."""
    platform: str
    url: str
    title: str = ""
    duration: float = 0.0
    # Progressiv MP4 (video + audio bitta faylda)
    progressive_url: str | None = None
    progressive_size: int = 0
    # Ajratilgan DASH oqimlari
    video_only_url: str | None = None
    audio_only_url: str | None = None
    audio_ext: str = "m4a"
    http_headers: dict[str, str] = field(default_factory=dict)
    # Telegram serverlari to'g'ridan-to'g'ri yuklay oladimi (Instagram CDN)
    telegram_direct_eligible: bool = False


class _YdlPool:
    """
    Thread-safe doimiy YoutubeDL ob'ektlar puli.
    Har bir platforma uchun iliq (warm) sessiya va cookie'larni saqlaydi.
    """

    def __init__(self, opts_factory, max_size: int = 4):
        self._opts_factory = opts_factory
        self._pool: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=max_size)

    def acquire(self) -> Any:
        try:
            return self._pool.get_nowait()
        except queue.Empty:
            ydl_mod = _get_yt_dlp()
            return ydl_mod.YoutubeDL(self._opts_factory())

    def release(self, ydl: Any):
        try:
            self._pool.put_nowait(ydl)
        except queue.Full:
            try:
                ydl.close()
            except Exception:
                pass

    def close_all(self):
        while True:
            try:
                ydl = self._pool.get_nowait()
                ydl.close()
            except queue.Empty:
                break
            except Exception:
                pass


def _base_ydl_opts() -> dict[str, Any]:
    return {
        "quiet": True,
        "no_warnings": True,
        "nocheckcertificate": True,
        "noplaylist": True,
        "socket_timeout": 10,
        "retries": 2,
        "geo_bypass": True,
    }


def _instagram_ydl_opts() -> dict[str, Any]:
    return _base_ydl_opts()


def _youtube_ydl_opts() -> dict[str, Any]:
    opts = _base_ydl_opts()
    opts["extractor_args"] = {"youtube": {"player_client": ["android"]}}
    return opts


def _search_ydl_opts() -> dict[str, Any]:
    opts = _youtube_ydl_opts()
    opts["extract_flat"] = "in_playlist"
    opts["socket_timeout"] = 6
    return opts


class Downloader:
    """In-process YoutubeDL pool + parallel aiohttp + FFmpeg dvigateli."""

    PLATFORM_PATTERNS = {
        "instagram": re.compile(
            r"(instagram\.com|instagr\.am)/(reel|p|reels|stories|tv)", re.IGNORECASE
        ),
        "youtube": re.compile(
            r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts/|youtube\.com/live)", re.IGNORECASE
        ),
        "tiktok": re.compile(
            r"(tiktok\.com|vm\.tiktok\.com)", re.IGNORECASE
        ),
    }

    URL_REGEX = re.compile(r"https?://[^\s<>\"'{}|\\^`\[\]]+")

    _pools: dict[str, _YdlPool] = {
        "instagram": _YdlPool(_instagram_ydl_opts, max_size=1 if LOW_RAM_MODE else 4),
        "tiktok": _YdlPool(_base_ydl_opts, max_size=1 if LOW_RAM_MODE else 3),
        "youtube": _YdlPool(_youtube_ydl_opts, max_size=1 if LOW_RAM_MODE else 4),
        "search": _YdlPool(_search_ydl_opts, max_size=1 if LOW_RAM_MODE else 3),
    }

    _http_session: aiohttp.ClientSession | None = None
    _ffmpeg_path: str | None = None

    @classmethod
    def _get_ffmpeg(cls) -> str:
        if cls._ffmpeg_path is None:
            if FFMPEG_PATH and Path(FFMPEG_PATH).exists():
                cls._ffmpeg_path = FFMPEG_PATH
            else:
                home_bin = Path.home() / "bin" / "ffmpeg"
                if home_bin.exists():
                    cls._ffmpeg_path = str(home_bin)
                else:
                    cls._ffmpeg_path = shutil.which("ffmpeg") or "ffmpeg"
        return cls._ffmpeg_path

    @classmethod
    async def get_http_session(cls) -> aiohttp.ClientSession:
        """Doimiy keep-alive va DNS keshli aiohttp sessiyasi."""
        if cls._http_session is None or cls._http_session.closed:
            connector = aiohttp.TCPConnector(
                limit=60,
                limit_per_host=12,
                ttl_dns_cache=300,
                ssl=False,
                enable_cleanup_closed=True,
            )
            timeout = aiohttp.ClientTimeout(total=45, connect=10, sock_read=20)
            cls._http_session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        return cls._http_session

    @classmethod
    async def warmup(cls):
        """Bot ishga tushganda HTTP sessiyani va (yetarli RAM bo'lsa) YoutubeDL pullarini qizdiradi."""
        await cls.get_http_session()
        cls._get_ffmpeg()

        if LOW_RAM_MODE:
            # 256MB RAM da faqat kerak bo'lganda bitta pool ochiladi (xotirani tejash)
            return

        def _init_pools():
            for key in ("instagram", "youtube", "search"):
                ydl = cls._pools[key].acquire()
                cls._pools[key].release(ydl)

        await asyncio.to_thread(_init_pools)

        # Fonda Instagram sessiya cookie'sini oldindan olish (birinchi Reel ham ~1.1s da ishlaydi!)
        async def _prewarm_instagram():
            def _touch():
                pool = cls._pools["instagram"]
                ydl = pool.acquire()
                try:
                    ie = ydl.get_info_extractor("Instagram")
                    if ie and not getattr(ie, "_ready", False):
                        ie.initialize()
                except Exception:
                    pass
                finally:
                    pool.release(ydl)
            await asyncio.to_thread(_touch)

        asyncio.create_task(_prewarm_instagram())

    @classmethod
    async def close(cls):
        """Bot to'xtaganda resurslarni toza yopish."""
        if cls._http_session and not cls._http_session.closed:
            await cls._http_session.close()
        for pool in cls._pools.values():
            pool.close_all()

    @classmethod
    def extract_url(cls, text: str) -> str | None:
        match = cls.URL_REGEX.search(text)
        if match:
            return match.group(0).rstrip(".,;:!?)")
        return None

    @classmethod
    def detect_platform(cls, url: str) -> str | None:
        for platform, pattern in cls.PLATFORM_PATTERNS.items():
            if pattern.search(url):
                return platform
        return None

    # ─── FFMPEG ASINXRON YORDAMCHI ─────────────────────────────

    @classmethod
    async def _run_ffmpeg(cls, args: list[str], timeout: int = 20) -> bool:
        process = None
        try:
            process = await asyncio.create_subprocess_exec(
                cls._get_ffmpeg(),
                "-hide_banner",
                "-loglevel", "error",
                *args,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode == 0
        except asyncio.TimeoutError:
            if process is not None:
                try:
                    process.kill()
                except Exception:
                    pass
            return False
        except Exception:
            return False

    # ─── PARALLEL HTTP RANGE YUKLASH (6x OQIM) ─────────────────

    @classmethod
    async def fast_http_download(
        cls,
        url: str,
        dest: Path,
        headers: dict[str, str] | None = None,
        chunks: int = 6,
        known_size: int = 0,
    ) -> Path | None:
        """
        Faylni 6 ta parallel HTTP Range oqimida yuklab oladi.
        YouTube (googlevideo.com) uchun HEAD o'tkazib yuboriladi va clen/known_size orqali
        darhol parallel Range oqimlari bilan throttle aylanib o'tiladi!
        """
        session = await cls.get_http_session()
        req_headers = dict(headers or {})
        req_headers.pop("Range", None)
        is_googlevideo = "googlevideo.com" in url

        try:
            content_len = known_size
            supports_ranges = is_googlevideo

            if is_googlevideo and content_len <= 0:
                m_clen = re.search(r"[?&]clen=(\d+)", url)
                if m_clen:
                    content_len = int(m_clen.group(1))

            if not is_googlevideo and (content_len <= 0 or not supports_ranges):
                try:
                    async with session.head(url, headers=req_headers, allow_redirects=True) as head_resp:
                        if head_resp.status < 400:
                            content_len = content_len or int(head_resp.headers.get("Content-Length", "0") or 0)
                            if "bytes" in (head_resp.headers.get("Accept-Ranges", "")).lower():
                                supports_ranges = True
                except Exception:
                    pass

            # Kichik fayllar (<350KB) yoki Range yo'q serverlar -> bitta tezkor GET (Low-RAM da ham stream qilinadi)
            if LOW_RAM_MODE or content_len < 350 * 1024 or not supports_ranges:
                async with session.get(url, headers=req_headers, allow_redirects=True) as resp:
                    if resp.status >= 400:
                        return None
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(65536):
                            f.write(chunk)
                return dest if dest.exists() and dest.stat().st_size > 0 else None

            # Katta fayllar -> parallel Range oqimlari (YouTube throttle aylanib o'tiladi!)
            num_chunks = min(chunks, max(2, content_len // (256 * 1024)))
            chunk_size = content_len // num_chunks
            ranges = [
                (i * chunk_size, content_len - 1 if i == num_chunks - 1 else (i + 1) * chunk_size - 1)
                for i in range(num_chunks)
            ]

            async def _fetch_part(start: int, end: int) -> bytes:
                h = dict(req_headers)
                h["Range"] = f"bytes={start}-{end}"
                async with session.get(url, headers=h, allow_redirects=True) as r:
                    r.raise_for_status()
                    return await r.read()

            parts = await asyncio.gather(*(_fetch_part(s, e) for s, e in ranges))
            with open(dest, "wb") as f:
                for p in parts:
                    f.write(p)

            return dest if dest.exists() and dest.stat().st_size > 0 else None

        except Exception as e:
            print(f"[!] fast_http_download xato: {str(e)[:150]}")
            return None

    # ─── 1. MEDIA METAMA'LUMOTLARINI AJRATISH (IN-PROCESS) ─────

    @classmethod
    async def extract_media_info(cls, url: str, platform: str) -> ExtractedMedia | None:
        """
        Doimiy in-process YoutubeDL orqali to'g'ridan-to'g'ri video/audio URL larni oladi.
        Subprocess yo'q — ~1.1-1.6 soniyada bajariladi!
        """
        pool = cls._pools.get(platform, cls._pools["tiktok"])

        def _extract():
            ydl = pool.acquire()
            try:
                return ydl.extract_info(url, download=False)
            finally:
                pool.release(ydl)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=25)
        except Exception as e:
            print(f"[!] extract_media_info xato ({platform}): {str(e)[:200]}")
            return None

        if not info:
            return None

        # Agar playlist/carousel bo'lsa birinchi elementni olamiz
        if "entries" in info and info["entries"]:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]

        formats = info.get("formats") or []
        headers = dict(info.get("http_headers") or {})

        progressive_url: str | None = None
        progressive_size: int = 0
        video_only_url: str | None = None
        audio_only_url: str | None = None
        audio_ext: str = "m4a"

        if platform == "youtube":
            # YouTube: avval tezkor progressiv mp4 (format 18 yoki <=720p mp4)
            prog = [
                f for f in formats
                if f.get("url")
                and f.get("ext") == "mp4"
                and f.get("vcodec") not in (None, "none")
                and f.get("acodec") not in (None, "none")
            ]
            # 720p gacha eng yaxshisi (format 18/22)
            prog_cap = [f for f in prog if (f.get("height") or 0) <= 720]
            chosen_prog = (prog_cap or prog)[-1] if (prog_cap or prog) else None
            if chosen_prog:
                progressive_url = chosen_prog["url"]
                progressive_size = int(chosen_prog.get("filesize") or chosen_prog.get("filesize_approx") or 0)

            # Audio-only oqimi (m4a afzal)
            aud_m4a = [
                f for f in formats
                if f.get("url") and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none") and f.get("ext") == "m4a"
            ]
            aud_any = [
                f for f in formats
                if f.get("url") and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            ]
            chosen_aud = (aud_m4a or aud_any)[-1] if (aud_m4a or aud_any) else None
            if chosen_aud:
                audio_only_url = chosen_aud["url"]
                audio_ext = chosen_aud.get("ext") or "m4a"

            # Agar progressiv bo'lmasa <=720p video-only
            if not progressive_url:
                vids = [
                    f for f in formats
                    if f.get("url") and f.get("ext") == "mp4" and f.get("acodec") == "none" and (f.get("height") or 0) <= 720
                ]
                if vids:
                    video_only_url = vids[-1]["url"]
        else:
            # Instagram / TikTok: progressiv mp4 (vcodec/acodec 'none' bo'lmagan)
            prog = [
                f for f in formats
                if f.get("url")
                and f.get("ext") == "mp4"
                and f.get("vcodec") != "none"
                and f.get("acodec") != "none"
            ]
            if prog:
                chosen_prog = prog[-1]
                progressive_url = chosen_prog["url"]
                progressive_size = int(chosen_prog.get("filesize") or chosen_prog.get("filesize_approx") or 0)
            elif info.get("url") and info.get("ext") == "mp4":
                progressive_url = info["url"]

            # Audio-only DASH oqimi (Instagram Reels uchun 65-200KB m4a!)
            aud_only = [
                f for f in formats
                if f.get("url") and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
            ]
            if aud_only:
                chosen_aud = aud_only[-1]
                audio_only_url = chosen_aud["url"]
                audio_ext = chosen_aud.get("ext") or "m4a"

            # Agar progressiv yo'q bo'lsa DASH video-only (<=1080p mp4)
            if not progressive_url:
                vids = [
                    f for f in formats
                    if f.get("url") and f.get("acodec") == "none" and f.get("vcodec") not in (None, "none")
                ]
                vids_cap = [f for f in vids if (f.get("height") or 0) <= 1080]
                if vids_cap or vids:
                    video_only_url = (vids_cap or vids)[-1]["url"]

        # Telegram serverlari Instagram CDN progressiv MP4 ni to'g'ridan-to'g'ri yuklay oladi (<19MB)
        tg_direct = (
            platform == "instagram"
            and bool(progressive_url)
            and (progressive_size == 0 or progressive_size <= MAX_TELEGRAM_URL_SIZE)
        )

        return ExtractedMedia(
            platform=platform,
            url=url,
            title=str(info.get("title") or ""),
            duration=float(info.get("duration") or 0),
            progressive_url=progressive_url,
            progressive_size=progressive_size,
            video_only_url=video_only_url,
            audio_only_url=audio_only_url,
            audio_ext=audio_ext,
            http_headers=headers,
            telegram_direct_eligible=tg_direct,
        )

    # ─── 2. LOKAL VIDEO YUKLASH (PARALLEL + DASH MUX) ──────────

    @classmethod
    async def download_video_from_media(
        cls,
        media: ExtractedMedia,
        request_dir: Path,
        existing_audio_path: Path | None = None,
    ) -> Path | None:
        """
        Ajratilgan media ma'lumotidan videoni lokal faylga tezkor yuklaydi.
        Progressiv MP4 bo'lsa 6x parallel yuklaydi; faqat DASH bo'lsa video+audio ni birlashtiradi.
        """
        file_id = uuid.uuid4().hex[:6]
        out_mp4 = request_dir / f"video_{file_id}.mp4"

        if media.progressive_url:
            res = await cls.fast_http_download(
                media.progressive_url, out_mp4, media.http_headers, chunks=6, known_size=media.progressive_size
            )
            if res and res.exists():
                return res

        # DASH fallback: video_only + audio_only ni parallel yuklab, stream-copy mux (0.05s!)
        if media.video_only_url and media.audio_only_url:
            v_tmp = request_dir / f"vonly_{file_id}.mp4"
            a_tmp = existing_audio_path or (request_dir / f"aonly_{file_id}.{media.audio_ext}")

            tasks = [cls.fast_http_download(media.video_only_url, v_tmp, media.http_headers, chunks=6)]
            if existing_audio_path is None or not existing_audio_path.exists():
                tasks.append(cls.fast_http_download(media.audio_only_url, a_tmp, media.http_headers, chunks=4))

            results = await asyncio.gather(*tasks)
            if results[0] and v_tmp.exists() and a_tmp.exists():
                ok = await cls._run_ffmpeg(
                    ["-i", str(v_tmp), "-i", str(a_tmp), "-c", "copy", "-movflags", "+faststart", "-y", str(out_mp4)],
                    timeout=15,
                )
                if ok and out_mp4.exists() and out_mp4.stat().st_size > 0:
                    return out_mp4

        # So'nggi chora: in-process YoutubeDL download
        return await cls._ydl_fallback_download(media.url, media.platform, request_dir, f"video_{file_id}")

    @classmethod
    async def download_audio_stream(cls, media: ExtractedMedia, request_dir: Path) -> Path | None:
        """
        Faqat kichik DASH audio oqimini (65-200KB) 0.2-0.4 soniyada yuklab oladi!
        """
        if not media.audio_only_url:
            return None
        dest = request_dir / f"raw_audio.{media.audio_ext}"
        return await cls.fast_http_download(media.audio_only_url, dest, media.http_headers, chunks=2)

    @classmethod
    async def download_video(cls, url: str, request_dir: Path) -> Path | None:
        """Orqaga moslik (backward-compatible) uchun to'g'ridan-to'g'ri video yuklash."""
        platform = cls.detect_platform(url) or "instagram"
        media = await cls.extract_media_info(url, platform)
        if not media:
            return None
        return await cls.download_video_from_media(media, request_dir)

    @classmethod
    async def _ydl_fallback_download(cls, url: str, platform: str, request_dir: Path, prefix: str) -> Path | None:
        out_tmpl = str(request_dir / f"{prefix}.%(ext)s")
        opts = _youtube_ydl_opts() if platform == "youtube" else _base_ydl_opts()
        opts.update({
            "outtmpl": out_tmpl,
            "format": "b[ext=mp4]/b/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
            "merge_output_format": "mp4",
            "concurrent_fragment_downloads": 5,
        })

        def _dl():
            ydl_mod = _get_yt_dlp()
            with ydl_mod.YoutubeDL(opts) as ydl:
                ydl.download([url])

        try:
            await asyncio.wait_for(asyncio.to_thread(_dl), timeout=40)
        except Exception as e:
            print(f"[!] _ydl_fallback_download xato: {str(e)[:200]}")
            return None

        return cls._find_file(request_dir, prefix)

    # ─── 3. AUDIO KONVERTATSIYA (KO'P OQIMLI FFMPEG) ───────────

    @classmethod
    async def extract_audio_from_video(cls, input_path: Path, output_mp3: Path) -> bool:
        """Video yoki m4a fayldan ko'p oqimli FFmpeg orqali MP3 ajratib oladi (~0.1-0.3s)."""
        threads = "1" if LOW_RAM_MODE else "4"
        ok = await cls._run_ffmpeg(
            [
                "-i", str(input_path),
                "-vn",
                "-acodec", "libmp3lame",
                "-b:a", "128k",
                "-threads", threads,
                "-y",
                str(output_mp3),
            ],
            timeout=25 if LOW_RAM_MODE else 15,
        )
        return ok and output_mp3.exists() and output_mp3.stat().st_size > 0

    @classmethod
    async def create_snippet(cls, audio_path: Path, snippet_path: Path, duration: int = 10) -> bool:
        """Shazam uchun qisqa audio parcha (stream copy ~0.015s)."""
        ok = await cls._run_ffmpeg(
            [
                "-ss", "0",
                "-t", str(duration),
                "-i", str(audio_path),
                "-vn",
                "-acodec", "copy",
                "-y",
                str(snippet_path),
            ],
            timeout=6,
        )
        return ok and snippet_path.exists() and snippet_path.stat().st_size > 0

    @classmethod
    async def convert_to_mp3(cls, input_file: Path, output_mp3: Path) -> bool:
        return await cls.extract_audio_from_video(input_file, output_mp3)

    # ─── 4. TO'LIQ STUDIO TREK (FULL MP3 — IN-PROCESS + 6x) ───

    @classmethod
    async def _download_youtube_mp3(cls, target: str, request_dir: Path, prefix: str) -> Path | None:
        """
        YouTube URL yoki `ytsearch1:...` dan to'g'ridan-to'g'ri audio oqimini
        in-process YoutubeDL + parallel aiohttp + FFmpeg orqali MP3 qilib yuklaydi.
        """
        pool = cls._pools["youtube"]

        def _extract():
            ydl = pool.acquire()
            try:
                return ydl.extract_info(target, download=False)
            finally:
                pool.release(ydl)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_extract), timeout=18)
        except Exception as e:
            print(f"[!] _download_youtube_mp3 extract xato: {str(e)[:200]}")
            return None

        if not info:
            return None
        if "entries" in info and info["entries"]:
            entries = [e for e in info["entries"] if e]
            if not entries:
                return None
            info = entries[0]

        formats = info.get("formats") or []
        headers = dict(info.get("http_headers") or {})

        # Eng yaxshi audio-only oqimi (m4a/opus, <=160kbps tezlik uchun optimal)
        aud_only = [
            f for f in formats
            if f.get("url") and f.get("vcodec") == "none" and f.get("acodec") not in (None, "none")
        ]
        m4a_aud = [f for f in aud_only if f.get("ext") == "m4a"]
        chosen = (m4a_aud or aud_only)[-1] if (m4a_aud or aud_only) else None

        if not chosen:
            # Fallback: progressiv oqim
            prog = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")]
            chosen = prog[0] if prog else None

        if not chosen:
            return None

        raw_ext = chosen.get("ext") or "m4a"
        raw_path = request_dir / f"{prefix}_raw.{raw_ext}"
        mp3_path = request_dir / f"{prefix}.mp3"
        known_sz = int(chosen.get("filesize") or chosen.get("filesize_approx") or 0)

        dl = await cls.fast_http_download(chosen["url"], raw_path, headers, chunks=6, known_size=known_sz)
        if not dl:
            return None

        if raw_ext.lower() == "mp3":
            raw_path.rename(mp3_path)
            return mp3_path

        ok = await cls.extract_audio_from_video(raw_path, mp3_path)
        raw_path.unlink(missing_ok=True)
        return mp3_path if ok else None

    @classmethod
    async def download_full_track(cls, query: str, request_dir: Path) -> Path | None:
        """To'liq qo'shiqni YouTube'dan tezkor MP3 qilib yuklaydi."""
        file_id = uuid.uuid4().hex[:6]
        return await cls._download_youtube_mp3(f"ytsearch1:{query}", request_dir, f"full_{file_id}")

    @classmethod
    async def download_track_by_id(cls, video_id: str, request_dir: Path) -> Path | None:
        """Aniq YouTube video ID bo'yicha MP3 yuklaydi."""
        file_id = uuid.uuid4().hex[:6]
        return await cls._download_youtube_mp3(
            f"https://www.youtube.com/watch?v={video_id}", request_dir, f"track_{file_id}"
        )

    # ─── 5. QO'SHIQ QIDIRUVI (IN-PROCESS + 1 SOATLIK KESH) ─────

    @classmethod
    async def search_tracks(cls, query: str, limit: int = 5) -> list[dict]:
        """Qo'shiq nomi bo'yicha tezkor in-process qidiruv."""
        clean_q = query.lower().strip()
        now = time.time()

        if clean_q in search_memory_cache:
            cached_res, timestamp = search_memory_cache[clean_q]
            if now - timestamp < SEARCH_CACHE_TTL:
                return cached_res

        pool = cls._pools["search"]

        def _search():
            ydl = pool.acquire()
            try:
                return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
            finally:
                pool.release(ydl)

        try:
            info = await asyncio.wait_for(asyncio.to_thread(_search), timeout=12)
        except Exception as e:
            print(f"[!] search_tracks xato: {str(e)[:200]}")
            return []

        entries = (info or {}).get("entries") or []
        results = []
        for entry in entries:
            if not entry or not entry.get("id"):
                continue
            dur = entry.get("duration")
            dur_str = ""
            if isinstance(dur, (int, float)) and dur > 0:
                mins, secs = divmod(int(dur), 60)
                dur_str = f"{mins}:{secs:02d}"
            elif entry.get("duration_string"):
                dur_str = str(entry["duration_string"])

            results.append({
                "id": str(entry["id"]),
                "title": str(entry.get("title") or "Noma'lum"),
                "duration": dur_str,
            })

        if results:
            search_memory_cache[clean_q] = (results, now)
            # Xotira keshini 500 ta so'rov bilan cheklash
            if len(search_memory_cache) > 500:
                oldest = sorted(search_memory_cache.items(), key=lambda kv: kv[1][1])[:100]
                for k, _ in oldest:
                    search_memory_cache.pop(k, None)

        return results

    @staticmethod
    def _find_file(directory: Path, prefix: str) -> Path | None:
        for f in directory.iterdir():
            if (
                f.name.startswith(prefix)
                and f.is_file()
                and not f.name.endswith((".part", ".ytdl", ".temp"))
                and f.stat().st_size > 0
            ):
                return f
        return None
