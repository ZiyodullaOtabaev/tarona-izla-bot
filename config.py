import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

# Bot token from BotFather
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
BOT_USERNAME = os.getenv("BOT_USERNAME", "taronaizlauzbot")

# Admins (.env orqali sozlanadi)
PRIMARY_ADMIN_ID = int(os.getenv("PRIMARY_ADMIN_ID", "0") or 0)
ADMIN_IDS = [
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", str(PRIMARY_ADMIN_ID) if PRIMARY_ADMIN_ID else "").split(",")
    if x.strip().isdigit()
]
if PRIMARY_ADMIN_ID and PRIMARY_ADMIN_ID not in ADMIN_IDS:
    ADMIN_IDS.append(PRIMARY_ADMIN_ID)

ADMIN_USERNAMES = [
    u.strip().lower().lstrip("@")
    for u in os.getenv("ADMIN_USERNAMES", "").split(",")
    if u.strip()
]

# Dinamik adminlar keshi (bazadan yuklanadi)
DYNAMIC_ADMIN_IDS: set[int] = set()
DYNAMIC_ADMIN_USERNAMES: set[str] = set()


def is_owner(user_id: int) -> bool:
    """Foydalanuvchi asosiy admin (Ega) ekanligini tekshiradi."""
    return user_id == PRIMARY_ADMIN_ID


def is_admin(user_id: int, username: str | None = None) -> bool:
    """Foydalanuvchi admin ekanligini tekshiradi (statik + dinamik baza)."""
    if user_id == PRIMARY_ADMIN_ID or user_id in ADMIN_IDS or user_id in DYNAMIC_ADMIN_IDS:
        return True
    if username:
        clean = username.lower().lstrip("@")
        if clean in ADMIN_USERNAMES or clean in DYNAMIC_ADMIN_USERNAMES:
            return True
    return False


def sync_dynamic_admins(admin_rows: list[dict]):
    """Bazadagi adminlar ro'yxatini xotiraga sinxronlaydi."""
    DYNAMIC_ADMIN_IDS.clear()
    DYNAMIC_ADMIN_USERNAMES.clear()
    for row in admin_rows:
        uid = row.get("user_id")
        if uid:
            DYNAMIC_ADMIN_IDS.add(int(uid))
        uname = row.get("username")
        if uname:
            DYNAMIC_ADMIN_USERNAMES.add(str(uname).lower().lstrip("@"))


# Paths
BASE_DIR = Path(__file__).parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
DB_PATH = BASE_DIR / "tarona.db"

# Telegram limits
MAX_TELEGRAM_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

# Speed optimization: only first 15 seconds needed for Shazam
AUDIO_CLIP_DURATION = 15

# Low-RAM / Alwaysdata rejimi (256MB RAM + 0.25 CPU uchun tejamkor rejim)
LOW_RAM_MODE = os.getenv("LOW_RAM_MODE", "0").strip().lower() in ("1", "true", "yes")
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "").strip()
if FFMPEG_PATH:
    _ff_dir = str(Path(FFMPEG_PATH).parent)
    if _ff_dir not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{_ff_dir}{os.pathsep}{os.environ.get('PATH', '')}"

# Webhook sozlamalari (Alwaysdata Web -> Sites -> User program uchun; bo'sh bo'lsa polling ishlaydi)
WEBHOOK_HOST = os.getenv("WEBHOOK_HOST", "").strip().rstrip("/")
WEBHOOK_PATH = os.getenv("WEBHOOK_PATH", "/webhook").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "").strip()
WEB_HOST = os.getenv("IP", os.getenv("WEB_HOST", "::"))
WEB_PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8100")))

# Max concurrent downloads (default, can be tuned live from Admin Panel)
MAX_CONCURRENT = 1 if LOW_RAM_MODE else 3

# Cache TTL (seconds)
CACHE_TTL = 1800  # 30 minutes

# Broadcast speed limit (messages per second to avoid Telegram FloodWait)
BROADCAST_RATE_LIMIT = 25

# Dinamik boshqaruv sozlamalarining standart qiymatlari
DEFAULT_SETTINGS: dict[str, str] = {
    "maintenance_mode": "0",
    "maintenance_text": (
        "🛠 <b>Botda texnik ishlar olib borilmoqda.</b>\n\n"
        "Birozdan so'ng qayta urinib ko'ring!"
    ),
    "enable_instagram": "1",
    "enable_tiktok": "1",
    "enable_youtube": "1",
    "enable_voice": "1",
    "enable_search": "1",
    "cache_enabled": "1",
    "max_concurrent": str(MAX_CONCURRENT),
    "rate_limit_per_min": "10",
    "ad_footer": "",
    "welcome_text": "",
    "error_alerts": "1",
}
