"""Download and enrich the Uthmani Quran corpus."""

from __future__ import annotations

import json
from pathlib import Path

from datasets import load_dataset

OUTPUT = Path(__file__).resolve().parent / "data" / "quran.json"

# arbml/quran_uthmani omits Al-Fatihah ayah 1 (Bismillah) and numbers the rest 2–7.
FATIHAH_AYAH_1 = "بِسْمِ ٱللَّهِ ٱلرَّحْمَٰنِ ٱلرَّحِيمِ"

# Standard surah names (Arabic + commonly used English transliterations).
# Corpus text remains from arbml/quran_uthmani; names are metadata enrichment only.
SURAH_META: dict[int, tuple[str, str]] = {
    1: ("الفاتحة", "Al-Fatihah"),
    2: ("البقرة", "Al-Baqarah"),
    3: ("آل عمران", "Ali 'Imran"),
    4: ("النساء", "An-Nisa"),
    5: ("المائدة", "Al-Ma'idah"),
    6: ("الأنعام", "Al-An'am"),
    7: ("الأعراف", "Al-A'raf"),
    8: ("الأنفال", "Al-Anfal"),
    9: ("التوبة", "At-Tawbah"),
    10: ("يونس", "Yunus"),
    11: ("هود", "Hud"),
    12: ("يوسف", "Yusuf"),
    13: ("الرعد", "Ar-Ra'd"),
    14: ("إبراهيم", "Ibrahim"),
    15: ("الحجر", "Al-Hijr"),
    16: ("النحل", "An-Nahl"),
    17: ("الإسراء", "Al-Isra"),
    18: ("الكهف", "Al-Kahf"),
    19: ("مريم", "Maryam"),
    20: ("طه", "Taha"),
    21: ("الأنبياء", "Al-Anbiya"),
    22: ("الحج", "Al-Hajj"),
    23: ("المؤمنون", "Al-Mu'minun"),
    24: ("النور", "An-Nur"),
    25: ("الفرقان", "Al-Furqan"),
    26: ("الشعراء", "Ash-Shu'ara"),
    27: ("النمل", "An-Naml"),
    28: ("القصص", "Al-Qasas"),
    29: ("العنكبوت", "Al-'Ankabut"),
    30: ("الروم", "Ar-Rum"),
    31: ("لقمان", "Luqman"),
    32: ("السجدة", "As-Sajdah"),
    33: ("الأحزاب", "Al-Ahzab"),
    34: ("سبأ", "Saba"),
    35: ("فاطر", "Fatir"),
    36: ("يس", "Ya-Sin"),
    37: ("الصافات", "As-Saffat"),
    38: ("ص", "Sad"),
    39: ("الزمر", "Az-Zumar"),
    40: ("غافر", "Ghafir"),
    41: ("فصلت", "Fussilat"),
    42: ("الشورى", "Ash-Shura"),
    43: ("الزخرف", "Az-Zukhruf"),
    44: ("الدخان", "Ad-Dukhan"),
    45: ("الجاثية", "Al-Jathiyah"),
    46: ("الأحقاف", "Al-Ahqaf"),
    47: ("محمد", "Muhammad"),
    48: ("الفتح", "Al-Fath"),
    49: ("الحجرات", "Al-Hujurat"),
    50: ("ق", "Qaf"),
    51: ("الذاريات", "Adh-Dhariyat"),
    52: ("الطور", "At-Tur"),
    53: ("النجم", "An-Najm"),
    54: ("القمر", "Al-Qamar"),
    55: ("الرحمن", "Ar-Rahman"),
    56: ("الواقعة", "Al-Waqi'ah"),
    57: ("الحديد", "Al-Hadid"),
    58: ("المجادلة", "Al-Mujadila"),
    59: ("الحشر", "Al-Hashr"),
    60: ("الممتحنة", "Al-Mumtahanah"),
    61: ("الصف", "As-Saff"),
    62: ("الجمعة", "Al-Jumu'ah"),
    63: ("المنافقون", "Al-Munafiqun"),
    64: ("التغابن", "At-Taghabun"),
    65: ("الطلاق", "At-Talaq"),
    66: ("التحريم", "At-Tahrim"),
    67: ("الملك", "Al-Mulk"),
    68: ("القلم", "Al-Qalam"),
    69: ("الحاقة", "Al-Haqqah"),
    70: ("المعارج", "Al-Ma'arij"),
    71: ("نوح", "Nuh"),
    72: ("الجن", "Al-Jinn"),
    73: ("المزمل", "Al-Muzzammil"),
    74: ("المدثر", "Al-Muddaththir"),
    75: ("القيامة", "Al-Qiyamah"),
    76: ("الإنسان", "Al-Insan"),
    77: ("المرسلات", "Al-Mursalat"),
    78: ("النبأ", "An-Naba"),
    79: ("النازعات", "An-Nazi'at"),
    80: ("عبس", "Abasa"),
    81: ("التكوير", "At-Takwir"),
    82: ("الانفطار", "Al-Infitar"),
    83: ("المطففين", "Al-Mutaffifin"),
    84: ("الانشقاق", "Al-Inshiqaq"),
    85: ("البروج", "Al-Buruj"),
    86: ("الطارق", "At-Tariq"),
    87: ("الأعلى", "Al-A'la"),
    88: ("الغاشية", "Al-Ghashiyah"),
    89: ("الفجر", "Al-Fajr"),
    90: ("البلد", "Al-Balad"),
    91: ("الشمس", "Ash-Shams"),
    92: ("الليل", "Al-Layl"),
    93: ("الضحى", "Ad-Duhaa"),
    94: ("الشرح", "Ash-Sharh"),
    95: ("التين", "At-Tin"),
    96: ("العلق", "Al-'Alaq"),
    97: ("القدر", "Al-Qadr"),
    98: ("البينة", "Al-Bayyinah"),
    99: ("الزلزلة", "Az-Zalzalah"),
    100: ("العاديات", "Al-'Adiyat"),
    101: ("القارعة", "Al-Qari'ah"),
    102: ("التكاثر", "At-Takathur"),
    103: ("العصر", "Al-'Asr"),
    104: ("الهمزة", "Al-Humazah"),
    105: ("الفيل", "Al-Fil"),
    106: ("قريش", "Quraysh"),
    107: ("الماعون", "Al-Ma'un"),
    108: ("الكوثر", "Al-Kawthar"),
    109: ("الكافرون", "Al-Kafirun"),
    110: ("النصر", "An-Nasr"),
    111: ("المسد", "Al-Masad"),
    112: ("الإخلاص", "Al-Ikhlas"),
    113: ("الفلق", "Al-Falaq"),
    114: ("الناس", "An-Nas"),
}


def ensure_fatihah_complete(surahs: list[dict]) -> bool:
    """
    Insert Al-Fatihah ayah 1 if missing.

    Returns True when the corpus was modified.
    """
    for surah in surahs:
        if surah.get("number") != 1:
            continue
        ayahs = surah.get("ayahs") or []
        if any(int(a["number"]) == 1 for a in ayahs):
            return False
        ayahs.append({"number": 1, "text": FATIHAH_AYAH_1})
        ayahs.sort(key=lambda a: int(a["number"]))
        surah["ayahs"] = ayahs
        return True
    return False


def repair_corpus(path: Path = OUTPUT) -> bool:
    """Fix known corpus gaps in an existing quran.json. Returns True if changed."""
    if not path.is_file():
        return False
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    changed = ensure_fatihah_complete(data)
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"Repaired Al-Fatihah ayah 1 in {path}")
    return changed


def main() -> None:
    print("Downloading Quran corpus from arbml/quran_uthmani...")
    dataset = load_dataset("arbml/quran_uthmani", split="train")

    surahs: dict[int, dict] = {}
    for row in dataset:
        surah_number = int(row["sorah"])
        ayah_number = int(row["ayah"])
        name, english_name = SURAH_META.get(surah_number, ("", ""))

        if surah_number not in surahs:
            surahs[surah_number] = {
                "number": surah_number,
                "name": name,
                "english_name": english_name,
                "ayahs": [],
            }

        surahs[surah_number]["ayahs"].append(
            {
                "number": ayah_number,
                "text": row["sentence"],
            }
        )

    result = sorted(surahs.values(), key=lambda s: s["number"])
    for surah in result:
        surah["ayahs"].sort(key=lambda a: a["number"])

    if ensure_fatihah_complete(result):
        print("Inserted missing Al-Fatihah ayah 1 (Bismillah).")

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    ayah_count = sum(len(s["ayahs"]) for s in result)
    print(f"Saved {len(result)} surahs ({ayah_count} ayahs) to {OUTPUT}")
    print(
        "NOTE: Verify against a trusted Uthmani source "
        "(e.g. Tanzil / nuqayah/quran-text) before production use."
    )


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "--repair":
        if not repair_corpus():
            print("Corpus already complete (or missing).")
    else:
        main()
