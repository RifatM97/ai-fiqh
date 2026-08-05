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
    """True for characters that are unmappable font garbage, not real content.

    **Known defect, measured rather than assumed.** The 0x250 cutoff predates
    needing Latin Extended Additional (U+1E00-U+1EFF), which is where the
    dot-below letters this transliteration relies on live -- ``ṣ``, ``ḍ``,
    ``ḥ``, ``ṭ``, ``ẓ``. All are reported as junk. The macron letters ``ā``,
    ``ī``, ``ū`` sit below the cutoff and pass, so the function is inconsistent
    about the very scheme the corpus is written in.

    It survives because every caller applies a *ratio* threshold, and a few
    diacritics in normal-length text dilute below it. Measured cost across the
    whole corpus:

    * ``strip_junk_lines`` at 0.30 drops 72 lines, 71 of them genuine mojibake.
      The exception is p109's ``'q̣aḍā’'`` (ratio 0.33) -- the wrapped second
      half of a chapter heading. Harmless in the end: the heading still matched
      on its first line, the full title survives in the chunk's ``bab``, and the
      phrase recurs in the body text.
    * revision's 0.08 ceiling rejects exactly 2 items, both genuinely
      unmappable Arabic.

    Not fixed deliberately: correcting the range would change chunk text, which
    forces a re-ingest, which invalidates the embeddings and the golden set's
    ``expected_chunk_ids`` -- a wide blast radius to recover one heading
    fragment. Revisit if the corpus is ever re-ingested for another reason.

    Do not use this per-character to filter text for display -- it will eat
    ``Ṣalāh``. Use `display_title` for that.
    """
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


# Codepoints with no glyph in any normal font: the Private Use Area (where this
# PDF's ﷺ landed) and the C0/C1 control blocks. Deliberately *not* `is_junk_char`
# -- that one also flags ṣ, ḍ and ḥ, which is safe behind a ratio threshold and
# ruinous per character. See its docstring.
_UNRENDERABLE = re.compile(
    "["
    "\\ue000-\\uf8ff"    # Private Use Area — this PDF's unmapped SAW glyph lives here
    "\\u0000-\\u0008"    # C0 controls, keeping \\t and \\n
    "\\u000b-\\u001f"
    "\\u007f-\\u009f"    # DEL and C1 controls
    "\\ufff0-\\uffff"    # specials, incl. the replacement character
    "]"
)


def display_title(s: str) -> str:
    """A section title fit to put in front of a person.

    Chunk metadata carries the book's headings verbatim, and one of them ends in
    an unmappable glyph -- the Hajj chapter on visiting the Prophet ends in the
    Private Use Area codepoint that should have been ﷺ. Fine as an internal key,
    since every comparison sees the same bytes, but it renders as a blank box in
    a dropdown. Stripped only at the display boundary; never upstream, where the
    raw title is what `get_section` matches on.

    Transliteration is left completely alone -- ``The Book of Ṣalāh`` comes back
    unchanged, which is the whole reason this does not reuse `is_junk_char`.
    """
    return _WS.sub(" ", _UNRENDERABLE.sub("", s)).strip()


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
