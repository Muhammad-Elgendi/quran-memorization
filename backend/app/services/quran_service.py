import json
from pathlib import Path
from typing import Any, Optional


class QuranService:
    def __init__(self, path: Path):
        self.path = Path(path)
        if not self.path.is_file():
            raise FileNotFoundError(
                f"Quran corpus not found at {self.path}. "
                "Run: python backend/download_quran.py"
            )
        self.data = self._load()

    def _load(self) -> list[dict[str, Any]]:
        with open(self.path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_surahs(self) -> list[dict[str, Any]]:
        return [
            {
                "number": surah["number"],
                "name": surah["name"],
                "english_name": surah.get("english_name"),
                "ayah_count": len(surah["ayahs"]),
            }
            for surah in self.data
        ]

    def get_surah(self, surah_number: int) -> Optional[dict[str, Any]]:
        for surah in self.data:
            if surah["number"] == surah_number:
                return surah
        return None

    def get_ayah(
        self,
        surah_number: int,
        ayah_number: int,
    ) -> Optional[dict[str, Any]]:
        surah = self.get_surah(surah_number)
        if not surah:
            return None
        for ayah in surah["ayahs"]:
            if ayah["number"] == ayah_number:
                return ayah
        return None

    def get_range(
        self,
        surah_number: int,
        start_ayah: int,
        end_ayah: int,
    ) -> list[dict[str, Any]]:
        surah = self.get_surah(surah_number)
        if not surah:
            return []
        return [
            ayah
            for ayah in surah["ayahs"]
            if start_ayah <= ayah["number"] <= end_ayah
        ]
