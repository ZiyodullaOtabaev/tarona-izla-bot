"""
Musiqa aniqlash moduli (ShazamIO — tekin, API key kerak emas).

ShazamIO Shazam'ning ichki API'sini ishlatadi.
Bazada millionlab qo'shiqlar bor, shu jumladan o'zbek qo'shiqlar ham.
"""

from dataclasses import dataclass, field
from pathlib import Path

from shazamio import Shazam


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
    """Shazam asosidagi musiqa aniqlagich."""

    def __init__(self):
        self.shazam = Shazam()

    async def recognize(self, audio_path: Path | str) -> MusicResult:
        """
        Audio fayldan musiqani aniqlaydi.

        Args:
            audio_path: Audio fayl yo'li (.mp3, .m4a, .wav va boshqalar)

        Returns:
            MusicResult: Aniqlangan qo'shiq ma'lumotlari
        """
        try:
            result = await self.shazam.recognize(str(audio_path))

            # Agar natija bo'lmasa
            if not result or "track" not in result:
                return MusicResult(found=False)

            track = result["track"]

            # Asosiy ma'lumotlar
            title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum")

            # Album, yil va label — sections ichidan
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

            # Cover rasm
            images = track.get("images", {})
            cover_url = images.get("coverart", images.get("background", ""))

            # Shazam havolasi
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
