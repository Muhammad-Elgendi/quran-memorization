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

    def last_ayah_number(self, surah_number: int) -> Optional[int]:
        surah = self.get_surah(surah_number)
        if not surah or not surah["ayahs"]:
            return None
        return int(surah["ayahs"][-1]["number"])

    def next_ayah(
        self,
        surah_number: int,
        ayah_number: int,
        *,
        cross_surah: bool = False,
    ) -> Optional[tuple[int, int]]:
        last = self.last_ayah_number(surah_number)
        if last is None:
            return None
        if ayah_number < last:
            return surah_number, ayah_number + 1
        if not cross_surah:
            return None
        ordered = sorted(self.data, key=lambda s: s["number"])
        for idx, surah in enumerate(ordered):
            if surah["number"] != surah_number:
                continue
            if idx + 1 >= len(ordered):
                return None
            nxt = ordered[idx + 1]
            if not nxt.get("ayahs"):
                return None
            return int(nxt["number"]), int(nxt["ayahs"][0]["number"])
        return None

    @staticmethod
    def corpus_order(surah: int, ayah: int) -> tuple[int, int]:
        return (surah, ayah)
