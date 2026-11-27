"""
Musiqa aniqlash moduli (ShazamIO — Lazy-load bilan tezkor start).
"""

import asyncio
from dataclasses import dataclass
from pathlib import Path


@dataclass
class MusicResult:
    """Musiqa aniqlash natijasi."""

    found: bool = False
    title: str = ""
    artist: str = ""
    album: str = ""
    year: str = ""
    label: str = ""
    cover_url: str = ""
    shazam_url: str = ""


class MusicRecognizer:
    """Shazam asosidagi musiqa aniqlagich (startni sekinlashtirmaslik uchun lazy-import)."""

    def __init__(self):
        self._shazam = None

    def _get_shazam(self):
        if self._shazam is None:
            from shazamio import Shazam
            self._shazam = Shazam()
        return self._shazam

    async def warmup(self):
        """Fonda Shazam kutubxonasini oldindan xotiraga yuklab qo'yish."""
        await asyncio.to_thread(self._get_shazam)

    async def recognize(self, audio_path: Path | str) -> MusicResult:
        try:
            shazam = await asyncio.to_thread(self._get_shazam)
            result = await shazam.recognize(str(audio_path))

            if not result or "track" not in result:
                return MusicResult(found=False)

            track = result["track"]
            title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum")

            album = ""
            year = ""
            label = ""
            for section in track.get("sections", []):
                if section.get("type") == "SONG":
                    for meta in section.get("metadata", []):
                        meta_title = meta.get("title", "")
                        meta_text = meta.get("text", "")
                        if meta_title == "Album":
                            album = meta_text
                        elif meta_title == "Released":
                            year = meta_text
                        elif meta_title == "Label":
                            label = meta_text

            images = track.get("images", {})
            cover_url = images.get("coverart", images.get("background", ""))
            shazam_url = track.get("url", "")

            return MusicResult(
                found=True,
                title=title,
                artist=artist,
                album=album,
                year=year,
                label=label,
                cover_url=cover_url,
                shazam_url=shazam_url,
            )

        except Exception as e:
            print(f"⚠️ Shazam xatolik: {e}")
            return MusicResult(found=False)
