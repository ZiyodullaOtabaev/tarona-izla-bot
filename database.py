"""
Ma'lumotlar bazasi moduli (SQLite + aiosqlite — Doimiy ulanish, WAL rejimi, <1ms tezlik).

Optimizatsiyalar:
- Bitta doimiy aiosqlite ulanishi (har so'rovda yangi thread/ulanish ochilmaydi).
- PRAGMA journal_mode=WAL, synchronous=NORMAL, temp_store=MEMORY, cache_size=-16000.
- Sozlamalar va faol homiy kanallar xotirada keshlanadi (0ms o'qish).
- Mavjud tarona.db uchun xavfsiz avtomatik migratsiya.
"""

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Any

import aiosqlite

from config import DB_PATH, DEFAULT_SETTINGS, LOW_RAM_MODE, sync_dynamic_admins


class Database:
    def __init__(self, db_path=DB_PATH):
        self.db_path = db_path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._settings_cache: dict[str, str] = dict(DEFAULT_SETTINGS)
        self._active_channels_cache: list[dict[str, Any]] | None = None
        self._banned_ids: set[int] = set()

    @asynccontextmanager
    async def _session(self):
        """Doimiy SQLite ulanishini xavfsiz lock bilan taqdim etadi."""
        async with self._lock:
            if self._conn is None:
                self._conn = await aiosqlite.connect(self.db_path)
                self._conn.row_factory = aiosqlite.Row
                jmode = "TRUNCATE" if LOW_RAM_MODE else "WAL"
                await self._conn.execute(f"PRAGMA journal_mode = {jmode}")
                await self._conn.execute("PRAGMA synchronous = NORMAL")
                await self._conn.execute("PRAGMA temp_store = MEMORY")
                await self._conn.execute("PRAGMA cache_size = -16000")
                await self._conn.execute("PRAGMA busy_timeout = 5000")
            yield self._conn

    async def close(self):
        """Bot to'xtaganda bazani toza yopadi."""
        async with self._lock:
            if self._conn is not None:
                await self._conn.close()
                self._conn = None

    async def _ensure_column(self, conn: aiosqlite.Connection, table: str, column: str, ddl: str):
        cursor = await conn.execute(f"PRAGMA table_info({table})")
        cols = {row[1] for row in await cursor.fetchall()}
        if column not in cols:
            await conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    async def init_db(self):
        """Ma'lumotlar bazasi, jadvallar va migratsiyalarni ishga tushirish."""
        async with self._session() as conn:
            # 1. Foydalanuvchilar
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    joined_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_banned INTEGER DEFAULT 0,
                    is_active INTEGER DEFAULT 1,
                    ref_code TEXT DEFAULT NULL,
                    ban_reason TEXT DEFAULT NULL,
                    downloads_count INTEGER DEFAULT 0
                )
            """)
            await self._ensure_column(conn, "users", "ref_code", "TEXT DEFAULT NULL")
            await self._ensure_column(conn, "users", "ban_reason", "TEXT DEFAULT NULL")
            await self._ensure_column(conn, "users", "downloads_count", "INTEGER DEFAULT 0")

            # 2. Majburiy obuna (Homiy kanallar 2.0)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id TEXT UNIQUE,
                    title TEXT,
                    invite_link TEXT,
                    is_active INTEGER DEFAULT 1,
                    target_joins INTEGER DEFAULT 0,
                    joined_count INTEGER DEFAULT 0,
                    check_sub INTEGER DEFAULT 1,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self._ensure_column(conn, "channels", "target_joins", "INTEGER DEFAULT 0")
            await self._ensure_column(conn, "channels", "joined_count", "INTEGER DEFAULT 0")
            await self._ensure_column(conn, "channels", "check_sub", "INTEGER DEFAULT 1")

            # 3. Yuklashlar jurnali
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS downloads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    platform TEXT,
                    content_type TEXT,
                    from_cache INTEGER DEFAULT 0,
                    detail TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            await self._ensure_column(conn, "downloads", "from_cache", "INTEGER DEFAULT 0")
            await self._ensure_column(conn, "downloads", "detail", "TEXT DEFAULT ''")

            # 4. Media kesh (composite PK: cache_key + media_type)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS media_cache (
                    cache_key TEXT NOT NULL,
                    file_id TEXT,
                    media_type TEXT NOT NULL,
                    title TEXT,
                    performer TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (cache_key, media_type)
                )
            """)
            cursor = await conn.execute("PRAGMA table_info(media_cache)")
            pk_cols = [row[1] for row in await cursor.fetchall() if row[5] > 0]
            if pk_cols == ["cache_key"]:
                await conn.execute("""
                    CREATE TABLE IF NOT EXISTS media_cache_v2 (
                        cache_key TEXT NOT NULL,
                        file_id TEXT,
                        media_type TEXT NOT NULL,
                        title TEXT,
                        performer TEXT,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (cache_key, media_type)
                    )
                """)
                await conn.execute("""
                    INSERT OR IGNORE INTO media_cache_v2 (cache_key, file_id, media_type, title, performer, created_at)
                    SELECT cache_key, file_id, COALESCE(media_type, 'video'), title, performer, created_at
                    FROM media_cache
                """)
                await conn.execute("DROP TABLE media_cache")
                await conn.execute("ALTER TABLE media_cache_v2 RENAME TO media_cache")

            # 5. Dinamik sozlamalar
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 6. Dinamik adminlar
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS admins (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    added_by INTEGER,
                    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 7. Reklama / UTM havolalari
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS referral_links (
                    code TEXT PRIMARY KEY,
                    title TEXT,
                    created_by INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 8. Rassilkalar tarixi
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    audience TEXT,
                    mode TEXT,
                    total INTEGER DEFAULT 0,
                    sent INTEGER DEFAULT 0,
                    blocked INTEGER DEFAULT 0,
                    status TEXT DEFAULT 'running',
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP
                )
            """)

            # 9. Xatolar jurnali
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS error_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT,
                    error_text TEXT,
                    user_id INTEGER,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 10. Audit log
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    admin_name TEXT,
                    action TEXT,
                    details TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # 11. Tugma tokenlari (restartdan keyin ham tugmalar ishlashi uchun)
            await conn.execute("""
                CREATE TABLE IF NOT EXISTS button_tokens (
                    token TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (token, kind)
                )
            """)

            # Indekslar
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_created ON downloads(created_at)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_downloads_user ON downloads(user_id)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_active ON users(last_active)")
            await conn.execute("CREATE INDEX IF NOT EXISTS idx_users_ref ON users(ref_code)")

            await conn.execute("""
                UPDATE users
                SET downloads_count = (
                    SELECT COUNT(*) FROM downloads d WHERE d.user_id = users.user_id
                )
                WHERE downloads_count = 0
                  AND EXISTS (SELECT 1 FROM downloads d WHERE d.user_id = users.user_id)
            """)

            # Shazamsiz tushib qolgan eski kesh yozuvlarini avtomatik tozalash
            await conn.execute("""
                DELETE FROM media_cache
                WHERE cache_key IN (
                    SELECT cache_key FROM media_cache
                    WHERE media_type = 'audio' AND (performer = '@taronaizlauzbot' OR performer = '' OR performer IS NULL)
                )
            """)

            b_cur = await conn.execute("SELECT user_id FROM users WHERE is_banned = 1")
            self._banned_ids = {int(r[0]) for r in await b_cur.fetchall()}

            await conn.commit()

        await self.load_settings_cache()
        await self.sync_admins_cache()
        await self._refresh_channels_cache()

    # ─── DINAMIK SOZLAMALAR ─────────────────────────────────────

    async def load_settings_cache(self):
        merged = dict(DEFAULT_SETTINGS)
        async with self._session() as conn:
            cursor = await conn.execute("SELECT key, value FROM settings")
            for row in await cursor.fetchall():
                merged[row["key"]] = row["value"]
        self._settings_cache = merged

    def get_setting(self, key: str, default: str = "") -> str:
        return self._settings_cache.get(key, DEFAULT_SETTINGS.get(key, default))

    def get_bool_setting(self, key: str, default: bool = True) -> bool:
        val = self.get_setting(key, "1" if default else "0")
        return str(val).strip().lower() in ("1", "true", "yes", "on")

    def get_int_setting(self, key: str, default: int = 0) -> int:
        try:
            return int(self.get_setting(key, str(default)))
        except (ValueError, TypeError):
            return default

    async def set_setting(self, key: str, value: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute("""
                INSERT INTO settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value = excluded.value,
                    updated_at = excluded.updated_at
            """, (key, str(value), now))
            await conn.commit()
        self._settings_cache[key] = str(value)

    def get_all_settings(self) -> dict[str, str]:
        return dict(self._settings_cache)

    # ─── FOYDALANUVCHILAR BOSHQARUVI (CRM) ──────────────────────

    async def add_or_update_user(
        self,
        user_id: int,
        username: str | None,
        first_name: str | None,
        ref_code: str | None = None,
    ) -> dict[str, Any] | None:
        """
        Foydalanuvchini bazaga qo'shadi/yangilaydi va joriy user qatorini qaytaradi
        (bitta tranzaksiyada — ortiqcha SELECT yo'q!).
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, joined_at, last_active, is_active, ref_code)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_active = excluded.last_active,
                    is_active = 1
            """, (user_id, username, first_name, now, now, ref_code))
            await conn.commit()
            cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_user(self, user_id: int) -> dict[str, Any] | None:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def search_users(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        clean = query.strip().lstrip("@")
        async with self._session() as conn:
            if clean.isdigit():
                cursor = await conn.execute("SELECT * FROM users WHERE user_id = ? LIMIT ?", (int(clean), limit))
                rows = await cursor.fetchall()
                if rows:
                    return [dict(r) for r in rows]

            like = f"%{clean}%"
            cursor = await conn.execute(
                """
                SELECT * FROM users
                WHERE LOWER(COALESCE(username, '')) LIKE LOWER(?)
                   OR LOWER(COALESCE(first_name, '')) LIKE LOWER(?)
                ORDER BY last_active DESC
                LIMIT ?
                """,
                (like, like, limit),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_user_download_breakdown(self, user_id: int) -> dict[str, int]:
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT platform, COUNT(*) FROM downloads WHERE user_id = ? GROUP BY platform",
                (user_id,),
            )
            return {row[0]: row[1] for row in await cursor.fetchall()}

    async def get_top_users(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users ORDER BY downloads_count DESC, last_active DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    async def get_recent_users(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM users ORDER BY joined_at DESC LIMIT ?", (limit,))
            return [dict(r) for r in await cursor.fetchall()]

    async def get_banned_users(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT * FROM users WHERE is_banned = 1 ORDER BY last_active DESC LIMIT ?",
                (limit,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    def is_user_banned_fast(self, user_id: int) -> bool:
        """0ms xotiradan ban tekshiruvi (callback tugmalar uchun)."""
        return user_id in self._banned_ids

    async def set_user_ban(self, user_id: int, is_banned: bool, reason: str | None = None):
        async with self._session() as conn:
            await conn.execute(
                "UPDATE users SET is_banned = ?, ban_reason = ? WHERE user_id = ?",
                (1 if is_banned else 0, reason if is_banned else None, user_id),
            )
            await conn.commit()
        if is_banned:
            self._banned_ids.add(int(user_id))
        else:
            self._banned_ids.discard(int(user_id))

    async def save_button_token(self, token: str, kind: str, payload: Any):
        raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        async with self._session() as conn:
            await conn.execute(
                "INSERT OR REPLACE INTO button_tokens (token, kind, payload) VALUES (?, ?, ?)",
                (token, kind, raw),
            )
            await conn.execute(
                """
                DELETE FROM button_tokens
                WHERE rowid NOT IN (SELECT rowid FROM button_tokens ORDER BY rowid DESC LIMIT 2000)
                """
            )
            await conn.commit()

    async def get_button_token(self, token: str, kind: str) -> Any | None:
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT payload FROM button_tokens WHERE token = ? AND kind = ?",
                (token, kind),
            )
            row = await cursor.fetchone()
            if not row:
                return None
            raw = row[0]
            if kind == "search_list":
                try:
                    return json.loads(raw)
                except Exception:
                    return None
            return raw

    async def set_user_active(self, user_id: int, is_active: bool):
        async with self._session() as conn:
            await conn.execute(
                "UPDATE users SET is_active = ? WHERE user_id = ?",
                (1 if is_active else 0, user_id),
            )
            await conn.commit()

    async def get_all_user_ids(self, only_active: bool = False) -> list[int]:
        return await self.get_target_user_ids("active" if only_active else "all")

    async def get_target_user_ids(self, audience: str = "active") -> list[int]:
        async with self._session() as conn:
            if audience == "24h":
                since = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = await conn.execute(
                    "SELECT user_id FROM users WHERE is_banned = 0 AND is_active = 1 AND last_active >= ?",
                    (since,),
                )
            elif audience == "7d":
                since = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                cursor = await conn.execute(
                    "SELECT user_id FROM users WHERE is_banned = 0 AND is_active = 1 AND last_active >= ?",
                    (since,),
                )
            elif audience == "all":
                cursor = await conn.execute("SELECT user_id FROM users WHERE is_banned = 0")
            else:
                cursor = await conn.execute("SELECT user_id FROM users WHERE is_banned = 0 AND is_active = 1")
            return [row[0] for row in await cursor.fetchall()]

    async def get_all_users_for_export(self) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute(
                """
                SELECT user_id, username, first_name, joined_at, last_active,
                       is_active, is_banned, ref_code, downloads_count
                FROM users
                ORDER BY joined_at DESC
                """
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ─── YUKLASHLAR JURNALI ─────────────────────────────────────

    async def log_download(
        self,
        user_id: int,
        platform: str,
        content_type: str = "video",
        from_cache: bool = False,
        detail: str = "",
    ):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute(
                """
                INSERT INTO downloads (user_id, platform, content_type, from_cache, detail, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (user_id, platform, content_type, 1 if from_cache else 0, detail[:150], now),
            )
            await conn.execute(
                "UPDATE users SET downloads_count = COALESCE(downloads_count, 0) + 1 WHERE user_id = ?",
                (user_id,),
            )
            await conn.commit()

    async def get_recent_downloads(self, limit: int = 15) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute(
                """
                SELECT d.id, d.user_id, d.platform, d.content_type, d.from_cache, d.detail, d.created_at,
                       u.first_name, u.username
                FROM downloads d
                LEFT JOIN users u ON u.user_id = d.user_id
                ORDER BY d.id DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ─── JONLI ANALITIKA VA GRAFIKLAR ───────────────────────────

    async def get_stats(self) -> dict[str, Any]:
        now = datetime.now()
        today_str = now.strftime("%Y-%m-%d")
        yesterday_date = (now - timedelta(days=1)).strftime("%Y-%m-%d")
        since_24h = (now - timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        since_7d = (now - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        since_30d = (now - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")

        async with self._session() as conn:
            u_row = await (
                await conn.execute(
                    """
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(CASE WHEN date(joined_at) = date(?) THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN date(joined_at) = date(?) THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN last_active >= ? THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN last_active >= ? THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN last_active >= ? THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN is_active = 0 THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN is_banned = 1 THEN 1 ELSE 0 END), 0)
                    FROM users
                    """,
                    (today_str, yesterday_date, since_24h, since_7d, since_30d),
                )
            ).fetchone()

            d_row = await (
                await conn.execute(
                    """
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(CASE WHEN date(created_at) = date(?) THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN date(created_at) = date(?) THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN created_at >= ? THEN 1 ELSE 0 END), 0),
                        COALESCE(SUM(CASE WHEN from_cache = 1 THEN 1 ELSE 0 END), 0)
                    FROM downloads
                    """,
                    (today_str, yesterday_date, since_7d),
                )
            ).fetchone()

            platforms_raw = await (
                await conn.execute("SELECT platform, COUNT(*) FROM downloads GROUP BY platform")
            ).fetchall()
            platform_stats = {row[0]: row[1] for row in platforms_raw}

            return {
                "total_users": u_row[0],
                "today_users": u_row[1],
                "yesterday_users": u_row[2],
                "active_24h": u_row[3],
                "active_7d": u_row[4],
                "active_30d": u_row[5],
                "dead_users": u_row[6],
                "banned_users": u_row[7],
                "total_downloads": d_row[0],
                "today_downloads": d_row[1],
                "yesterday_downloads": d_row[2],
                "week_downloads": d_row[3],
                "cache_hits": d_row[4],
                "platform_stats": platform_stats,
            }

    async def get_daily_growth(self, days: int = 7) -> list[dict[str, Any]]:
        result = []
        now = datetime.now()
        async with self._session() as conn:
            for offset in range(days - 1, -1, -1):
                day = (now - timedelta(days=offset)).strftime("%Y-%m-%d")
                label = (now - timedelta(days=offset)).strftime("%d.%m")
                u_cnt = (
                    await (await conn.execute("SELECT COUNT(*) FROM users WHERE date(joined_at) = date(?)", (day,))).fetchone()
                )[0]
                d_cnt = (
                    await (await conn.execute("SELECT COUNT(*) FROM downloads WHERE date(created_at) = date(?)", (day,))).fetchone()
                )[0]
                result.append({"date": day, "label": label, "users": u_cnt, "downloads": d_cnt})
        return result

    # ─── HOMIY KANALLAR 2.0 (OPM + XOTIRA KESHI) ───────────────

    async def _refresh_channels_cache(self):
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM channels WHERE is_active = 1 ORDER BY id DESC")
            self._active_channels_cache = [dict(r) for r in await cursor.fetchall()]

    async def add_channel(
        self,
        channel_id: str,
        title: str,
        invite_link: str,
        target_joins: int = 0,
        check_sub: bool = True,
    ) -> bool:
        async with self._session() as conn:
            try:
                await conn.execute("""
                    INSERT INTO channels (channel_id, title, invite_link, is_active, target_joins, check_sub)
                    VALUES (?, ?, ?, 1, ?, ?)
                    ON CONFLICT(channel_id) DO UPDATE SET
                        title = excluded.title,
                        invite_link = excluded.invite_link,
                        is_active = 1,
                        target_joins = excluded.target_joins,
                        check_sub = excluded.check_sub
                """, (channel_id, title, invite_link, target_joins, 1 if check_sub else 0))
                await conn.commit()
            except Exception as e:
                print(f"Kanal qo'shishda xato: {e}")
                return False
        await self._refresh_channels_cache()
        return True

    async def get_channel(self, channel_id: str) -> dict[str, Any] | None:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM channels WHERE channel_id = ?", (channel_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_channel_by_db_id(self, db_id: int) -> dict[str, Any] | None:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM channels WHERE id = ?", (db_id,))
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def toggle_channel(self, db_id: int) -> bool | None:
        ch = await self.get_channel_by_db_id(db_id)
        if not ch:
            return None
        new_state = 0 if ch["is_active"] else 1
        async with self._session() as conn:
            await conn.execute("UPDATE channels SET is_active = ? WHERE id = ?", (new_state, db_id))
            await conn.commit()
        await self._refresh_channels_cache()
        return bool(new_state)

    async def set_channel_target(self, db_id: int, target_joins: int):
        async with self._session() as conn:
            await conn.execute(
                "UPDATE channels SET target_joins = ? WHERE id = ?",
                (max(0, target_joins), db_id),
            )
            await conn.commit()
        await self._refresh_channels_cache()

    async def reset_channel_count(self, db_id: int):
        async with self._session() as conn:
            await conn.execute("UPDATE channels SET joined_count = 0 WHERE id = ?", (db_id,))
            await conn.commit()

    async def increment_channel_join(self, channel_id: str) -> bool:
        auto_paused = False
        async with self._session() as conn:
            await conn.execute(
                "UPDATE channels SET joined_count = COALESCE(joined_count, 0) + 1 WHERE channel_id = ?",
                (channel_id,),
            )
            cursor = await conn.execute(
                "SELECT joined_count, target_joins, is_active FROM channels WHERE channel_id = ?",
                (channel_id,),
            )
            row = await cursor.fetchone()
            if row and row["is_active"] and row["target_joins"] > 0 and row["joined_count"] >= row["target_joins"]:
                await conn.execute("UPDATE channels SET is_active = 0 WHERE channel_id = ?", (channel_id,))
                auto_paused = True
            await conn.commit()
        if auto_paused:
            await self._refresh_channels_cache()
        return auto_paused

    async def remove_channel(self, channel_id: str):
        async with self._session() as conn:
            await conn.execute("DELETE FROM channels WHERE channel_id = ?", (channel_id,))
            await conn.commit()
        await self._refresh_channels_cache()

    async def remove_channel_by_db_id(self, db_id: int):
        async with self._session() as conn:
            await conn.execute("DELETE FROM channels WHERE id = ?", (db_id,))
            await conn.commit()
        await self._refresh_channels_cache()

    async def get_channels(self) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM channels ORDER BY id DESC")
            return [dict(r) for r in await cursor.fetchall()]

    async def get_active_channels(self) -> list[dict[str, Any]]:
        if self._active_channels_cache is None:
            await self._refresh_channels_cache()
        return list(self._active_channels_cache or [])

    # ─── REKLAMA / UTM HAVOLALARI ───────────────────────────────

    async def create_referral(self, code: str, title: str, created_by: int) -> bool:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            try:
                await conn.execute(
                    "INSERT INTO referral_links (code, title, created_by, created_at) VALUES (?, ?, ?, ?)",
                    (code.strip(), title.strip(), created_by, now),
                )
                await conn.commit()
                return True
            except Exception:
                return False

    async def delete_referral(self, code: str):
        async with self._session() as conn:
            await conn.execute("DELETE FROM referral_links WHERE code = ?", (code,))
            await conn.commit()

    async def get_referrals_with_stats(self) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute(
                """
                SELECT r.code, r.title, r.created_at,
                       COUNT(u.user_id) AS joined_users,
                       SUM(CASE WHEN u.is_active = 1 THEN 1 ELSE 0 END) AS alive_users,
                       COALESCE(SUM(u.downloads_count), 0) AS total_downloads
                FROM referral_links r
                LEFT JOIN users u ON u.ref_code = r.code
                GROUP BY r.code
                ORDER BY r.created_at DESC
                """
            )
            return [dict(r) for r in await cursor.fetchall()]

    # ─── RASSILKA TARIXI ────────────────────────────────────────

    async def create_broadcast_log(self, admin_id: int, audience: str, mode: str, total: int) -> int:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            cursor = await conn.execute(
                """
                INSERT INTO broadcasts (admin_id, audience, mode, total, status, started_at)
                VALUES (?, ?, ?, ?, 'running', ?)
                """,
                (admin_id, audience, mode, total, now),
            )
            await conn.commit()
            return cursor.lastrowid

    async def finish_broadcast_log(self, b_id: int, sent: int, blocked: int, status: str):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute(
                """
                UPDATE broadcasts
                SET sent = ?, blocked = ?, status = ?, finished_at = ?
                WHERE id = ?
                """,
                (sent, blocked, status, now, b_id),
            )
            await conn.commit()

    async def get_broadcast_history(self, limit: int = 10) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM broadcasts ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in await cursor.fetchall()]

    # ─── DINAMIK ADMINLAR ───────────────────────────────────────

    async def sync_admins_cache(self):
        admins = await self.get_dynamic_admins()
        sync_dynamic_admins(admins)

    async def get_dynamic_admins(self) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM admins ORDER BY added_at DESC")
            return [dict(r) for r in await cursor.fetchall()]

    async def add_dynamic_admin(self, user_id: int, username: str | None, first_name: str | None, added_by: int):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute(
                """
                INSERT INTO admins (user_id, username, first_name, added_by, added_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name
                """,
                (user_id, username, first_name, added_by, now),
            )
            await conn.commit()
        await self.sync_admins_cache()

    async def remove_dynamic_admin(self, user_id: int):
        async with self._session() as conn:
            await conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
            await conn.commit()
        await self.sync_admins_cache()

    # ─── XATOLAR VA AUDIT LOGLAR ────────────────────────────────

    async def log_error(self, source: str, error_text: str, user_id: int | None = None):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute(
                "INSERT INTO error_logs (source, error_text, user_id, created_at) VALUES (?, ?, ?, ?)",
                (source[:60], error_text[:500], user_id, now),
            )
            await conn.execute(
                "DELETE FROM error_logs WHERE id NOT IN (SELECT id FROM error_logs ORDER BY id DESC LIMIT 200)"
            )
            await conn.commit()

    async def get_recent_errors(self, limit: int = 15) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM error_logs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in await cursor.fetchall()]

    async def clear_errors(self):
        async with self._session() as conn:
            await conn.execute("DELETE FROM error_logs")
            await conn.commit()

    async def log_audit(self, admin_id: int, admin_name: str, action: str, details: str = ""):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute(
                "INSERT INTO audit_logs (admin_id, admin_name, action, details, created_at) VALUES (?, ?, ?, ?, ?)",
                (admin_id, admin_name[:60], action[:80], details[:300], now),
            )
            await conn.execute(
                "DELETE FROM audit_logs WHERE id NOT IN (SELECT id FROM audit_logs ORDER BY id DESC LIMIT 500)"
            )
            await conn.commit()

    async def get_audit_logs(self, limit: int = 20) -> list[dict[str, Any]]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (limit,))
            return [dict(r) for r in await cursor.fetchall()]

    # ─── TELEGRAM MEDIA KESHI (file_id) ─────────────────────────

    async def get_cached_media(self, cache_key: str, media_type: str) -> dict[str, Any] | None:
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT * FROM media_cache WHERE cache_key = ? AND media_type = ?",
                (cache_key.strip(), media_type),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_cached_pair(self, cache_key: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Video va audio keshni bitta so'rovda qaytaradi."""
        async with self._session() as conn:
            cursor = await conn.execute(
                "SELECT * FROM media_cache WHERE cache_key = ? AND media_type IN ('video', 'audio')",
                (cache_key.strip(),),
            )
            rows = await cursor.fetchall()
            vid = None
            aud = None
            for r in rows:
                d = dict(r)
                if d["media_type"] == "video":
                    vid = d
                elif d["media_type"] == "audio":
                    aud = d
            return vid, aud

    async def set_cached_media(
        self,
        cache_key: str,
        file_id: str,
        media_type: str,
        title: str = "",
        performer: str = "",
    ):
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        async with self._session() as conn:
            await conn.execute("""
                INSERT OR REPLACE INTO media_cache (cache_key, file_id, media_type, title, performer, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (cache_key.strip(), file_id, media_type, title, performer, now))
            await conn.commit()

    async def get_cache_stats(self) -> dict[str, int]:
        async with self._session() as conn:
            cursor = await conn.execute("SELECT media_type, COUNT(*) FROM media_cache GROUP BY media_type")
            by_type = {row[0]: row[1] for row in await cursor.fetchall()}
            total = sum(by_type.values())
            return {
                "total": total,
                "video": by_type.get("video", 0),
                "audio": by_type.get("audio", 0) + by_type.get("full_mp3", 0) + by_type.get("search_mp3", 0),
            }

    async def clear_media_cache(self) -> int:
        async with self._session() as conn:
            count = (await (await conn.execute("SELECT COUNT(*) FROM media_cache")).fetchone())[0]
            await conn.execute("DELETE FROM media_cache")
            await conn.commit()
            return count


# Global database obyekti
db = Database()
