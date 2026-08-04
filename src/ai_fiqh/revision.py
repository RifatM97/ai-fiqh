"""Revision mode — MCQs and flashcards (docs/research.md §2.3).

The distractor problem, restated: a generated MCQ is only useful if the wrong
answers are reliably wrong. Let Claude invent distractors and it will eventually
invent one that happens to be true, producing a question with two right answers
and teaching the user something false when they check the key.

The fix mirrors §1.3 — exploit the book's structure instead of trusting the
model. The book classifies every enumerated ruling into a legal category, so:

    correct answer   <- an item from (kitab, category)          e.g. fard of wudu
    distractors      <- items from (kitab, OTHER category)      e.g. sunnah, adab

A distractor drawn this way is *guaranteed* wrong, because the book itself files
it elsewhere. The options are then lifted verbatim from the book, so Claude's
whole job is writing the stem and the explanation -- it neither picks the answer
nor words the choices.

Polarity groups (§1.3) are the second source, and they make the best questions:
"Which of these does NOT break wuḍūʾ?" with three genuine nullifiers and one
non-nullifier is exactly what a fiqh exam tests.

    uv run python -m ai_fiqh.revision
"""

from __future__ import annotations

import random
import re
from collections import defaultdict
from typing import Any

from .index import Retriever, load_env
from .normalize import fold, junk_ratio
from .schemas import (
    Flashcard,
    GeneratedFlashcards,
    MCQ,
    MCQStem,
    SourceItem,
    ValidationFailure,
)

MODEL = "claude-opus-5"
EFFORT = "medium"  # writing a stem around fixed options, not deciding anything
MAX_TOKENS = 8_000

# Categories the book genuinely assigns. `general` is excluded: 133 of 177 chunks
# are topical ("Tayammum", "Chapter of Witr") rather than category-shaped, and a
# distractor drawn from `general` carries no guarantee of being wrong.
REAL_CATEGORIES = {
    "fard", "wajib", "sunnah", "adab", "makruh", "shurut",
    "nullifier", "non-nullifier",
}

# Which categories make a sound contrast. Pairing `fard` against `wajib` is a
# real exam distinction; pairing `nullifier` against `makruh` is just noise.
CONTRAST_SETS: list[set[str]] = [
    {"fard", "wajib", "sunnah", "adab", "makruh", "shurut"},
    {"nullifier", "non-nullifier"},
]

# The book enumerates two ways and is not consistent about it: the purity and
# prayer chapters use `1)`, the fasting chapters use `1.`. Matching only the
# first silently drops every item in the Book of Fasting -- which is the richest
# polarity material there is, since it splits three ways (§1.3).
_ITEM_START = re.compile(r"^(\d{1,3})(?:\s*&\s*\d{1,3})?[).]\s*(.+)$")
# A footnote dropped mid-list: `10 The word 'sunan' is the plural…`. No paren,
# so it would otherwise be swallowed as a continuation of the previous item.
_FOOTNOTE = re.compile(r"^\d{1,3}\s+[A-Z(‘\"]")

MIN_ITEM_CHARS = 12
MAX_ITEM_CHARS = 220

# A footnote marker welded to the word before it -- `tawarruk13`, `recited28`.
# The lookbehind is what keeps genuine numbers ("three verses", "2 rakāʿah")
# intact: only a digit run directly following a letter is a marker.
_GLUED_FOOTNOTE = re.compile(r"(?<=[^\W\d_])\d{1,2}\b")

# Some items name their own legal category ("It is sunnah for a female to sit in
# the tawarruk position"). Fine in the book, fatal as an MCQ distractor -- the
# option would announce that it is not the answer. 14 of 373 items are dropped
# for this, which is cheap next to handing the student the key.
_SELF_DECLARING = re.compile(
    r"\b(is|are)\s+(a\s+)?(fard|farḍ|wajib|wājib|sunnah|sunan|adab|makruh|makrūh|"
    r"mustahab|mustaḥab|shart|sharṭ|mandub|mandūb)\b",
    re.IGNORECASE,
)
# Above this share of unmappable glyphs the item is mostly broken Arabic (§1.6)
# and would read as gibberish in an answer option.
MAX_ITEM_JUNK = 0.08
# Flashcards only. MCQ options are lifted verbatim and need no such check; a
# flashcard's answer is genuinely written by the model, so it is worth asking
# what share of it traces back to the passage. Even here the measure is weak --
# calibration showed faithful rewording scores a median 57% overlap, because
# synonym substitution is indistinguishable from invention by word overlap --
# so this is set low and catches only answers that are largely untethered.
MIN_GROUNDING = 0.35

_STOPWORDS = set(
    """the a an of to in is it and or for on with that this these those which not no
    be if by as at from does do their there they he she his her its one two when what
    who how all any such other into upon over under after before during while even""".split()
)


def extract_items(chunk: dict) -> list[str]:
    """Pull the enumerated rulings out of a chunk, in order.

    Continuation lines belong to the item above them; footnote lines are dropped
    rather than glued onto whichever item they happened to interrupt.
    """
    items: list[list[str]] = []
    for line in chunk["text_raw"].split("\n"):
        line = line.strip()
        if not line or _FOOTNOTE.match(line):
            continue
        m = _ITEM_START.match(line)
        if m:
            items.append([m.group(2).strip()])
        elif items:
            items[-1].append(line)

    out = []
    for parts in items:
        text = re.sub(r"\s+", " ", " ".join(parts).strip())
        text = _GLUED_FOOTNOTE.sub("", text)
        if not MIN_ITEM_CHARS <= len(text) <= MAX_ITEM_CHARS:
            continue
        # Items carrying unmappable Arabic glyphs (§1.6) read as gibberish in an
        # answer option, so they are dropped rather than shown to a student.
        if junk_ratio(text) > MAX_ITEM_JUNK or len(_content_words(text)) < 3:
            continue
        if _SELF_DECLARING.search(text):
            continue
        out.append(text)
    return out


_SUFFIXES = ("ing", "ment", "ion", "ed", "es", "ly", "s")


def _stem(word: str) -> str:
    """Crude suffix stripper, enough to match a reinflected paraphrase.

    Rephrasing an item naturally changes verb forms -- the book's "To place the
    nose" becomes "Placing the nose" -- and comparing raw tokens scores that as
    fabrication. Stemming to a common root is what makes the grounding check
    measure meaning rather than morphology. Not linguistically principled; it
    only has to be consistent on both sides of the comparison.
    """
    for suffix in _SUFFIXES:
        if len(word) - len(suffix) >= 4 and word.endswith(suffix):
            word = word[: -len(suffix)]
            break
    return word.rstrip("e")[:5]


def _content_words(text: str) -> set[str]:
    return {
        _stem(w)
        for w in re.findall(r"[a-z]+", fold(text))
        if len(w) > 2 and w not in _STOPWORDS
    }


def build_item_pool(chunks: list[dict]) -> dict[str, list[SourceItem]]:
    """Every extractable ruling, bucketed by the category the book filed it under."""
    pool: dict[str, list[SourceItem]] = defaultdict(list)
    n = 0
    for chunk in chunks:
        if chunk["category"] not in REAL_CATEGORIES:
            continue
        for text in extract_items(chunk):
            pool[chunk["category"]].append(
                SourceItem(
                    index=n, text=text, chunk_id=chunk["id"],
                    category=chunk["category"], kitab=chunk["kitab"], bab=chunk["bab"],
                    page_start=chunk["page_start"], page_end=chunk["page_end"],
                )
            )
            n += 1
    return dict(pool)


def select_items(
    pool: dict[str, list[SourceItem]], kitab: str, category: str, rng: random.Random
) -> list[SourceItem] | None:
    """Choose one correct item and three guaranteed-wrong distractors.

    All selection happens here, in code. The distractors come from a *different*
    category inside the same kitab, so the book itself is the authority for their
    being wrong.
    """
    contrast = next((s for s in CONTRAST_SETS if category in s), None)
    if contrast is None:
        return None

    correct_pool = [i for i in pool.get(category, []) if i.kitab == kitab]
    wrong_pool = [
        i
        for cat in contrast - {category}
        for i in pool.get(cat, [])
        if i.kitab == kitab
    ]
    if not correct_pool or len(wrong_pool) < 3:
        return None

    correct = rng.choice(correct_pool)

    # Keep the four options comparable in length. Options come from the book
    # verbatim and its items run from a few words to a full sentence, so an
    # unconstrained draw can put a 37-character answer beside a 126-character
    # distractor -- a tell a student can play without knowing any fiqh. Widen
    # the band rather than fail if the kitab is too small to satisfy it.
    target = len(correct.text)
    for factor in (1.6, 2.4, None):
        if factor is None:
            banded = wrong_pool
            break
        banded = [i for i in wrong_pool if target / factor <= len(i.text) <= target * factor]
        if len(banded) >= 3:
            break

    # Distractors from as many distinct categories as available, so the question
    # isn't secretly a two-way contrast wearing four options.
    by_cat: dict[str, list[SourceItem]] = defaultdict(list)
    for i in banded:
        by_cat[i.category].append(i)
    distractors: list[SourceItem] = []
    for cat in sorted(by_cat, key=lambda c: -len(by_cat[c])):
        if len(distractors) < 3:
            distractors.append(rng.choice(by_cat[cat]))
    remaining = [i for i in banded if i not in distractors]
    while len(distractors) < 3 and remaining:
        pick = rng.choice(remaining)
        distractors.append(pick)
        remaining.remove(pick)

    return [correct, *distractors[:3]] if len(distractors) >= 3 else None


_MCQ_SYSTEM = """\
You write multiple-choice revision questions for students of Hanafi fiqh, from \
*Nur al-Idah*.

You will be given four lettered options, taken verbatim from the book, and told
which one is the correct answer. **The options are fixed — do not rewrite,
shorten, or re-order them, and do not return them.**

Write two things:

1. A question stem asking which option belongs to the stated legal category.
   Phrase it the way an exam would, and do not hint at which option is correct.
2. A one-sentence explanation of why the correct answer is correct according to
   the book. Name the category each wrong option actually belongs to — that is
   what makes the question useful for revision."""

_FLASHCARD_SYSTEM = """\
You write revision flashcards for students of Hanafi fiqh, from *Nur al-Idah*.

From the passage supplied, write flashcards that test recall of what the book
actually says. The front is a question or a term; the back is the ruling, stated
as the book states it.

Use only what the passage contains. Do not add conditions, numbers, or
qualifications that are not there. Prefer several precise cards over one card
that tries to cover a whole section."""


def _client(client: Any = None) -> Any:
    if client is not None:
        return client
    load_env()
    import anthropic

    return anthropic.Anthropic()


def generate_mcq(
    items: list[SourceItem], category: str, *, client: Any = None, rng: random.Random
) -> tuple[MCQ | None, list[ValidationFailure]]:
    """Write a stem around a pre-selected item set, then validate it."""
    shuffled = list(items)
    rng.shuffle(shuffled)
    correct_index = shuffled.index(items[0])

    listing = "\n".join(
        f"{'ABCD'[n]}) {option_text(i)}" for n, i in enumerate(shuffled)
    )
    user = (
        f"Legal category being tested: **{category}**\n"
        f"Correct answer: {'ABCD'[correct_index]}\n"
        f"Categories the book files each option under: "
        + ", ".join(f"{'ABCD'[n]}={i.category}" for n, i in enumerate(shuffled))
        + f"\n\nOPTIONS\n{listing}\n\nWrite the stem and the explanation."
    )

    response = _client(client).messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT},
        system=[{"type": "text", "text": _MCQ_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=MCQStem,
    )
    return assemble_mcq(response.parsed_output, shuffled, correct_index, category)


def option_text(item: SourceItem) -> str:
    """The book's own wording, tidied just enough to sit under a letter.

    The book writes its enumerations as infinitives ("To wash the entire body
    once"), which reads oddly as an answer option. Dropping the leading "To"
    and capitalising is the only change made -- no rewording, so the option is
    still verbatim source text and needs no grounding check.
    """
    text = re.sub(r"^To\s+", "", item.text).strip()
    text = re.sub(r"\d+$", "", text).strip()  # trailing footnote marker
    return text[:1].upper() + text[1:]


def assemble_mcq(
    stem: MCQStem,
    items: list[SourceItem],
    correct_index: int,
    category: str,
) -> tuple[MCQ | None, list[ValidationFailure]]:
    """The §2.3 checks that still apply once options are lifted verbatim.

    Checks 1 and 2 (option count, options tracing to real source items) are now
    satisfied by construction rather than by inspection -- the options *are* the
    source items, and there are four because this code chose four. What remains
    is what the code could still get wrong: a distractor that shares the answer's
    category, or a page attribution that does not match the items used.
    """
    fails: list[ValidationFailure] = []

    if len(items) != 4:
        fails.append(ValidationFailure(check="option_count",
                                       detail=f"{len(items)} items supplied"))
        return None, fails
    if not stem.question.strip():
        fails.append(ValidationFailure(check="empty_stem", detail="no question text"))
        return None, fails

    # every distractor is filed under a different category than the answer
    answer_cat = items[correct_index].category
    for n, item in enumerate(items):
        if n != correct_index and item.category == answer_cat:
            fails.append(ValidationFailure(
                check="distractor_category",
                detail=f"item {n} shares category {answer_cat!r} with the answer",
            ))

    # the cited page is one of the pages the items actually came from
    pages = {p for i in items for p in range(i.page_start, i.page_end + 1)}
    source_page = items[correct_index].page_start
    if source_page not in pages:
        fails.append(ValidationFailure(check="source_page",
                                       detail=f"p{source_page} not in {sorted(pages)}"))
    if fails:
        return None, fails

    return (
        MCQ(
            question=stem.question,
            options=[option_text(i) for i in items],
            correct_index=correct_index,
            explanation=stem.explanation,
            source_page=source_page,
            kitab=items[correct_index].kitab,
            bab=items[correct_index].bab,
            category=category,
            distractor_categories=sorted(
                {i.category for n, i in enumerate(items) if n != correct_index}
            ),
        ),
        [],
    )


def generate_mcqs(
    kitab: str,
    category: str,
    n: int = 3,
    *,
    retriever: Retriever | None = None,
    client: Any = None,
    seed: int = 0,
    max_attempts: int | None = None,
) -> tuple[list[MCQ], list[ValidationFailure]]:
    r = retriever if retriever is not None else Retriever(verbose=False)
    pool = build_item_pool(r.chunks)
    rng = random.Random(seed)
    client = _client(client)

    made: list[MCQ] = []
    fails: list[ValidationFailure] = []
    attempts = max_attempts if max_attempts is not None else n * 3
    seen: set[str] = set()

    for _ in range(attempts):
        if len(made) >= n:
            break
        items = select_items(pool, kitab, category, rng)
        if items is None:
            fails.append(ValidationFailure(
                check="no_pool", detail=f"no contrast pool for {kitab}/{category}"))
            break
        if items[0].text in seen:
            continue
        mcq, f = generate_mcq(items, category, client=client, rng=rng)
        fails.extend(f)
        if mcq:
            seen.add(items[0].text)
            made.append(mcq)
    return made, fails


def generate_flashcards(
    chunk_id: str,
    n: int = 4,
    *,
    retriever: Retriever | None = None,
    client: Any = None,
) -> tuple[list[Flashcard], list[ValidationFailure]]:
    """Cards from one section. Deterministic lookup — no retrieval involved."""
    r = retriever if retriever is not None else Retriever(verbose=False)
    parts = r.get_section(chunk_id)
    text = "\n".join(p["text_raw"] for p in parts)
    head = parts[0]

    response = _client(client).messages.parse(
        model=MODEL,
        max_tokens=MAX_TOKENS,
        output_config={"effort": EFFORT},
        system=[{"type": "text", "text": _FLASHCARD_SYSTEM,
                 "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user",
                   "content": f"Write {n} flashcards from this passage.\n\n{text}"}],
        output_format=GeneratedFlashcards,
    )

    pages = {p for part in parts for p in range(part["page_start"], part["page_end"] + 1)}
    source_words = _content_words(text)
    cards: list[Flashcard] = []
    fails: list[ValidationFailure] = []

    for card in response.parsed_output.cards:
        back_words = _content_words(card.back)
        overlap = len(back_words & source_words) / len(back_words) if back_words else 0.0
        if overlap < MIN_GROUNDING:
            fails.append(ValidationFailure(
                check="card_grounding",
                detail=f"{overlap:.0%} of the answer traces to the passage: {card.front!r}",
            ))
            continue
        cards.append(
            Flashcard(front=card.front, back=card.back, source_page=min(pages),
                      kitab=head["kitab"], bab=head["bab"])
        )
    return cards, fails


def main() -> None:
    r = Retriever(verbose=False)
    pool = build_item_pool(r.chunks)
    print("item pool by category:")
    for cat, items in sorted(pool.items(), key=lambda kv: -len(kv[1])):
        kitabs = sorted({i.kitab for i in items})
        print(f"  {cat:<15} {len(items):>3} items  {kitabs}")

    # Note `salah/fard` is deliberately absent: the four salah chunks the
    # classifier labelled `fard` are topical sections whose titles merely
    # contain the word ("Joining the farḍ prayer"), not enumerations, so they
    # yield no items. See the known-limitations note in the tracker.
    print("\n--- MCQs: wajib of salah, distractors from sunnah/shurut/adab/makruh ---")
    mcqs, fails = generate_mcqs("salah", "wajib", n=2, retriever=r, seed=7)
    for m in mcqs:
        print("\n" + m.render())

    print("\n--- MCQ: polarity, which does NOT break the fast ---")
    pol, f2 = generate_mcqs("sawm", "non-nullifier", n=1, retriever=r, seed=3)
    for m in pol:
        print("\n" + m.render())

    print("\n--- flashcards from the fard actions of ghusl ---")
    cards, f3 = generate_flashcards("019-the-fard-actions-of-ghusl", n=3, retriever=r)
    for c in cards:
        print("\n" + c.render())

    all_fails = fails + f2 + f3
    print(f"\n{len(all_fails)} validation failure(s)")
    for f in all_fails:
        print(f"  [{f.check}] {f.detail}")


if __name__ == "__main__":
    main()
