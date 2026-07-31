"""Ingestion: PDF -> structure-aware chunks.

One-time script, not on the runtime path. Run with `uv run python -m ai_fiqh.ingest`.

The book has no embedded PDF outline, but pages 1-4 carry a printed table of
contents whose page numbers are reliable. Those entries are the segmentation
anchors: one chunk per ToC section, sliced out of a flat line stream.

Three quirks of this particular PDF, all verified against the source:

* Every page's first text line is its own printed page number, so the
  printed-page -> PDF-index offset (1) can be asserted per page rather than
  assumed. This matters because a wrong offset silently corrupts every citation.
* Body headings carry inline footnote markers the ToC omits
  (`The sunan10 of wuḍū’`), so heading matching must ignore digits.
* The book's own ToC is off by one for `Sujūd al-tilāwah` (says 79, is on 80),
  so headings are located by a monotonic forward scan rather than by page.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import fitz

from .normalize import fold, strip_junk_lines

ROOT = Path(__file__).resolve().parents[2]
PDF_PATH = ROOT / "data" / "Fiqh Class Book.pdf"
OUT_DIR = ROOT / "index"

TOC_PAGE_INDICES = range(0, 4)
FIRST_CONTENT_INDEX = 4  # printed page 5, "Author's Preface"
_NUM = re.compile(r"^\d{1,3}$")

# Sections longer than this are sub-split on line boundaries. Structure gives us
# correct *boundaries*, but a few sections run to 11 pages, and a ~6k-token chunk
# buries its own answer and cannot be usefully reranked. Sub-chunks inherit all
# section metadata, so citations still resolve to the section.
MAX_CHUNK_CHARS = 2_500

# ToC titles that differ from the heading actually printed in the body. Fuzzy
# matching cannot bridge a genuinely different word, so these are declared.
# Written as natural text and folded at import -- writing pre-folded strings by
# hand is error-prone, since `fold` also strips hyphens and apostrophes.
_TITLE_OVERRIDES_RAW: dict[str, str] = {
    # ToC says "Sujūd al-tilāwah"; the body heading reads "Sajdat al-tilāwah".
    "Sujūd al-tilāwah": "Sajdat al-tilāwah",
}
TITLE_OVERRIDES: dict[str, str] = {
    fold(k, drop_digits=True): fold(v, drop_digits=True)
    for k, v in _TITLE_OVERRIDES_RAW.items()
}

# Printed page ranges for each kitab, read off the parsed ToC.
KITABS: list[tuple[int, int, str]] = [
    (6, 30, "taharah"),
    (31, 101, "salah"),
    (102, 122, "sawm"),
    (123, 131, "zakah"),
    (132, 163, "hajj"),
]

# Hand-curated polarity groups. These are the sections the book presents as
# contrasting sets; retrieval must always surface a whole group together, never
# one member alone (see docs/research.md §1.3). Keyed by folded ToC title.
#
# Note these are NOT all pairs -- fasting splits three ways.
GROUPS: dict[str, tuple[str, str]] = {
    "those things which nullify wudu": ("wudu-nullifiers", "affirmative"),
    "those things which do not break wudu": ("wudu-nullifiers", "negative"),
    "things which necessitate ghusl": ("ghusl-necessitate", "affirmative"),
    "things which do not necessitate ghusl": ("ghusl-necessitate", "negative"),
    "chapter regarding those things which nullify salah": ("salah-nullifiers", "affirmative"),
    "things which do not nullify salah": ("salah-nullifiers", "negative"),
    "makruh acts of salah": ("salah-makruh", "affirmative"),
    "those acts which arent makruh in salah": ("salah-makruh", "negative"),
    "chapter on those things which nullify the fast and necessitate kaffarah with qada": (
        "sawm-nullifiers", "affirmative"),
    "chapter on those things which nullify the fast without necessitating kaffarah": (
        "sawm-nullifiers", "affirmative"),
    "chapter on those things which do not nullify the fast": ("sawm-nullifiers", "negative"),
    "chapter on offences": ("hajj-penalty", "affirmative"),
    "chapter on those things which do not necessitate a penalty by killing them": (
        "hajj-penalty", "negative"),
}

# Order matters: negations are checked before their affirmative counterparts.
_CATEGORY_RULES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bdo(es)? not (nullify|break|necessitate)|arent makruh|do not break"), "non-nullifier"),
    (re.compile(r"\bnullify|\bbreak wudu|\bnecessitate"), "nullifier"),
    (re.compile(r"\bfard\b|\bfaraid\b|\bfard actions\b"), "fard"),
    (re.compile(r"\bwajibat\b|\bwajib\b"), "wajib"),
    (re.compile(r"\bsunan\b|\bsunnah\b|\bmasnun\b"), "sunnah"),
    (re.compile(r"\betiquettes?\b|\badab\b"), "adab"),
    (re.compile(r"\bmakruh|\bdisliked\b|\bmustahab"), "makruh"),
    (re.compile(r"\bprerequisites?\b|\bconditions?\b"), "shurut"),
]


@dataclass
class Chunk:
    id: str
    kitab: str
    bab: str
    category: str
    group_id: str | None
    polarity: str | None
    page_start: int
    page_end: int
    part: int
    n_parts: int
    text_raw: str
    text_folded: str


def _page_lines(doc: fitz.Document, index: int) -> list[str]:
    return [ln.strip() for ln in doc[index].get_text().split("\n") if ln.strip()]


def verify_page_offset(doc: fitz.Document) -> list[int]:
    """Assert printed page N lives at PDF index N-1. Returns offending indices."""
    bad = []
    for i in range(FIRST_CONTENT_INDEX, len(doc)):
        lines = _page_lines(doc, i)
        if not lines or lines[0] != str(i + 1):
            bad.append(i)
    return bad


def parse_toc(doc: fitz.Document) -> list[tuple[str, int]]:
    """Extract (title, printed_page) from the printed contents pages.

    Format is a run of title lines terminated by a bare number line; long titles
    wrap across several lines.
    """
    entries: list[tuple[str, int]] = []
    for p in TOC_PAGE_INDICES:
        lines = _page_lines(doc, p)
        if lines and _NUM.match(lines[0]):
            lines = lines[1:]  # the ToC page's own page number
        lines = [l for l in lines if l.lower() != "contents"]
        parts: list[str] = []
        for ln in lines:
            if _NUM.match(ln):
                if parts:
                    entries.append((" ".join(parts), int(ln)))
                    parts = []
            else:
                parts.append(ln)
    return entries


def build_line_stream(doc: fitz.Document) -> list[tuple[int, str]]:
    """Flatten the body into (printed_page, line), dropping page numbers and junk."""
    stream: list[tuple[int, str]] = []
    for i in range(FIRST_CONTENT_INDEX, len(doc)):
        printed = i + 1
        lines = _page_lines(doc, i)
        if lines and lines[0] == str(printed):
            lines = lines[1:]
        for ln in strip_junk_lines(lines):
            stream.append((printed, ln))
    return stream


def _heading_matches(line: str, title: str) -> bool:
    """Tolerant heading comparison: ignores diacritics, punctuation and digits."""
    fl, ft = fold(line, drop_digits=True), fold(title, drop_digits=True)
    ft = TITLE_OVERRIDES.get(ft, ft)
    if not fl or not ft:
        return False
    if fl == ft or fl.startswith(ft) or ft.startswith(fl):
        return True
    # Fall back to a prefix probe -- rescues titles containing unmappable glyphs
    # such as `Istisq̣āԒ`, where exact comparison can never succeed.
    words = ft.split()
    if len(words) >= 3:
        probe = " ".join(words[:3])
        return fl.startswith(probe) and abs(len(fl) - len(ft)) <= 12
    return False


def locate_headings(
    stream: list[tuple[int, str]], entries: list[tuple[str, int]]
) -> tuple[list[tuple[int, str, int]], list[tuple[str, int]]]:
    """Find each ToC heading in the line stream by monotonic forward scan.

    Scanning forward from the previous match (rather than searching each page)
    handles repeated headings, the book's off-by-one ToC entry, and sections
    that start mid-page -- all in one mechanism.
    """
    located: list[tuple[int, str, int]] = []  # (stream_index, title, printed_page)
    unlocated: list[tuple[str, int]] = []
    cursor = 0
    for title, printed in entries:
        hit = None
        for j in range(cursor, len(stream)):
            if _heading_matches(stream[j][1], title):
                hit = j
                break
        if hit is None:
            unlocated.append((title, printed))
            continue
        located.append((hit, title, stream[hit][0]))
        cursor = hit + 1
    return located, unlocated


def classify_category(title: str) -> str:
    folded = fold(title)
    for pattern, label in _CATEGORY_RULES:
        if pattern.search(folded):
            return label
    return "general"


def assign_kitab(printed_page: int) -> str:
    for lo, hi, name in KITABS:
        if lo <= printed_page <= hi:
            return name
    return "front-matter" if printed_page < 6 else "back-matter"


def slugify(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", fold(title, drop_digits=True)).strip("-")[:60]


def _split_body(
    body: list[tuple[int, str]], limit: int
) -> list[list[tuple[int, str]]]:
    """Break an oversized section into parts on line boundaries."""
    parts: list[list[tuple[int, str]]] = []
    current: list[tuple[int, str]] = []
    size = 0
    for page, line in body:
        if current and size + len(line) > limit:
            parts.append(current)
            current, size = [], 0
        current.append((page, line))
        size += len(line) + 1
    if current:
        parts.append(current)
    return parts or [body]


def build_chunks(
    stream: list[tuple[int, str]], located: list[tuple[int, str, int]]
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for n, (start, title, printed) in enumerate(located):
        end = located[n + 1][0] if n + 1 < len(located) else len(stream)
        body = stream[start:end]
        if not body:
            continue

        group_id, polarity = GROUPS.get(fold(title), (None, None))
        kitab = assign_kitab(printed)
        category = classify_category(title)
        slug = slugify(title)
        parts = _split_body(body, MAX_CHUNK_CHARS)

        for i, part in enumerate(parts):
            lines = [line for _, line in part]
            # Continuation parts get the section heading prepended so each chunk
            # is self-describing when embedded in isolation.
            text = "\n".join(lines if i == 0 else [title, *lines])
            pages = [p for p, _ in part]
            suffix = f"-p{i}" if len(parts) > 1 else ""
            chunks.append(
                Chunk(
                    id=f"{printed:03d}-{slug}{suffix}",
                    kitab=kitab,
                    bab=title,
                    category=category,
                    group_id=group_id,
                    polarity=polarity,
                    page_start=min(pages),
                    page_end=max(pages),
                    part=i,
                    n_parts=len(parts),
                    text_raw=text,
                    text_folded=fold(text),
                )
            )
    return chunks


def main() -> None:
    doc = fitz.open(PDF_PATH)

    bad = verify_page_offset(doc)
    if bad:
        raise SystemExit(
            f"Page offset assertion failed on PDF indices {bad[:10]}. "
            "Every citation depends on this; fix before continuing."
        )
    print(f"page offset verified on {len(doc) - FIRST_CONTENT_INDEX} content pages")

    entries = parse_toc(doc)
    stream = build_line_stream(doc)
    located, unlocated = locate_headings(stream, entries)

    print(f"toc entries      : {len(entries)}")
    print(f"headings located : {len(located)}")
    if unlocated:
        print(f"headings MISSING : {len(unlocated)}")
        for title, page in unlocated:
            print(f"    p{page:>3}  {title!r}")

    chunks = build_chunks(stream, located)

    # Every declared group member must have survived into a chunk, or pair
    # expansion silently degrades at query time.
    seen_groups: dict[str, list[str]] = {}
    for c in chunks:
        if c.group_id:
            seen_groups.setdefault(c.group_id, []).append(c.polarity or "?")
    expected = {gid for gid, _ in GROUPS.values()}
    missing = expected - seen_groups.keys()
    if missing:
        print(f"WARNING: group(s) never matched a chunk: {sorted(missing)}")

    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "chunks.json").write_text(
        json.dumps([asdict(c) for c in chunks], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(f"\nchunks written   : {len(chunks)} -> {OUT_DIR / 'chunks.json'}")
    print(f"groups resolved  : {len(seen_groups)}/{len(expected)}")
    for gid, pols in sorted(seen_groups.items()):
        print(f"    {gid:<20} {len(pols)} members {pols}")

    from collections import Counter

    print("\nby kitab   :", dict(Counter(c.kitab for c in chunks)))
    print("by category:", dict(Counter(c.category for c in chunks)))


if __name__ == "__main__":
    main()
