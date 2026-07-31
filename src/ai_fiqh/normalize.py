"""Text normalisation for a corpus of English prose with Arabic transliteration.

Two independent jobs:

1. `fold` — strip diacritics and punctuation so `wuḍūʾ`, `wudu'` and `WUDU` all
   compare equal. Used for BM25 indexing and heading matching, never for the
   text shown to the model.
2. `strip_junk_lines` — drop lines that are unmappable font garbage. The PDF's
   Arabic quotes extract as Latin-Extended/Cyrillic mojibake (`ُʧ`, `ٰѴَْ Ѵɴ`),
   about 1.4% of all characters, isolated on their own lines.
"""

from __future__ import annotations

import re
import unicodedata

# Characters the transliteration scheme uses legitimately, plus typographic
# punctuation. Everything else above Latin Extended-A is treated as garbage.
_LEGIT_HIGH = set("ʾʿ‘’“”—–…·")

_PUNCT = re.compile(r"[’'‘`ʾʿ\"“”.,:;()\[\]{}!?…—–\-/]")
_WS = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


def fold(s: str, *, drop_digits: bool = False) -> str:
    """Diacritic-insensitive, punctuation-insensitive, lowercase form.

    `drop_digits` additionally removes digits, which is needed when matching
    headings: the body text carries inline footnote markers that the table of
    contents does not (`The sunan10 of wuḍū’` vs `The sunan of wuḍū’`).
    """
    decomposed = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in decomposed if unicodedata.category(c) != "Mn")
    out = unicodedata.normalize("NFC", stripped)
    if drop_digits:
        out = _DIGITS.sub("", out)
    out = _PUNCT.sub("", out)
    return _WS.sub(" ", out).strip().lower()


def is_junk_char(c: str) -> bool:
    """True for characters that are unmappable font garbage, not real content."""
    if c.isspace() or c in _LEGIT_HIGH:
        return False
    o = ord(c)
    if o < 0x250:  # ASCII, Latin-1, Latin Extended-A — the transliteration range
        return False
    if 0x600 <= o <= 0x6FF:  # genuine Arabic block, if it ever maps correctly
        return False
    return True


def junk_ratio(s: str) -> float:
    """Fraction of non-whitespace characters that are font garbage."""
    chars = [c for c in s if not c.isspace()]
    if not chars:
        return 0.0
    return sum(1 for c in chars if is_junk_char(c)) / len(chars)


def strip_junk_lines(lines: list[str], threshold: float = 0.30) -> list[str]:
    """Drop lines that are mostly unmappable glyphs.

    The garbage is line-isolated in this corpus, so a line-level filter is
    sufficient and avoids mangling legitimate transliteration.
    """
    return [ln for ln in lines if junk_ratio(ln) < threshold]


# --- Arabic-term alias layer -------------------------------------------------
# `fold` handles diacritic variance (wuḍūʾ -> wudu). It cannot handle competing
# romanisation schemes (wudhu, wuzu), which need an explicit map. Keys and
# values are already in folded form.
ALIASES: dict[str, str] = {
    # purity
    "wudhu": "wudu", "wuzu": "wudu", "wudoo": "wudu", "ablution": "wudu",
    "ghusal": "ghusl", "gusl": "ghusl",
    "tayammom": "tayammum", "tayamum": "tayammum",
    "istinja": "istinja", "najasah": "najasat", "najaasah": "najasat",
    "haid": "haid", "hayd": "haid", "haiz": "haid", "menstruation": "haid",
    "nifaas": "nifas", "istihaadah": "istihadah",
    "masah": "masah", "mash": "masah",
    # salah
    "salaat": "salah", "salat": "salah", "namaz": "salah", "namaaz": "salah",
    "prayer": "salah", "sajdah": "sajdah", "sajda": "sajdah",
    "rakat": "rakah", "rakaat": "rakah", "rakah": "rakah", "ruku": "ruku",
    "adhaan": "adhan", "azan": "adhan", "iqamah": "iqamah", "iqaamah": "iqamah",
    "witir": "witr", "taraweeh": "tarawih", "taravih": "tarawih",
    "jumuah": "jumuah", "juma": "jumuah", "jumma": "jumuah",
    "janaza": "janazah", "janaazah": "janazah", "funeral": "janazah",
    "imamah": "imamah", "imaamah": "imamah",
    # fasting
    "sawm": "sawm", "saum": "sawm", "roza": "sawm", "fast": "sawm",
    "iftaar": "iftar", "suhoor": "sahur", "sehri": "sahur",
    "itikaf": "itikaf", "etikaf": "itikaf",
    "kaffaarah": "kaffarah", "kafarah": "kaffarah",
    "qadaa": "qada", "qazaa": "qada",
    # zakah
    "zakat": "zakah", "zakaat": "zakah", "zakaah": "zakah",
    "nisaab": "nisab", "sadaqah": "sadaqat", "fitrah": "fitr",
    # hajj
    "hajj": "hajj", "haj": "hajj", "umrah": "umrah", "umra": "umrah",
    "ihraam": "ihram", "tawaaf": "tawaf", "saee": "sai", "saiy": "sai",
    "qiran": "qiran", "tamattu": "tamattu",
    # legal categories
    "fardh": "fard", "farz": "fard", "faraid": "fard", "faraidh": "fard",
    "waajib": "wajib", "waajibaat": "wajib", "wajibat": "wajib",
    "sunnat": "sunnah", "sunan": "sunnah", "masnun": "sunnah",
    "makruh": "makruh", "makrooh": "makruh", "makroohat": "makruh",
    "mustahab": "mustahab", "mustahabb": "mustahab",
    "adaab": "adab", "aadaab": "adab", "etiquette": "adab",
    "haraam": "haram", "mandoob": "mandub",
}


def expand_aliases(query: str) -> str:
    """Append canonical forms of any recognised alias to the query.

    Used for BM25 query expansion only — appending rather than replacing keeps
    the user's own wording in play.
    """
    folded = fold(query)
    extra = [ALIASES[tok] for tok in folded.split() if tok in ALIASES]
    unique = [t for t in dict.fromkeys(extra) if t not in folded.split()]
    return folded + (" " + " ".join(unique) if unique else "")
