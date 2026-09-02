"""System prompts, versioned as module constants.

Kept in one file on purpose (docs/research.md §3.4): the abstention prompt is the
single thing in this project that gets iterated on most, and an eval loop is
tractable only when every version of it lives in one place.

Bump `QA_PROMPT_VERSION` on any edit to `QA_SYSTEM` and record the eval numbers
against it. A prompt change with no version bump makes past eval runs unreadable.
"""

from __future__ import annotations

QA_PROMPT_VERSION = "qa-v1"

# Layer 3 of §1.7. Note what is deliberately absent: no instruction to
# double-check or verify its own answer.
#
# That omission was calibrated against Opus 5, which self-verifies unprompted, so
# instructing it again produced over-verification rather than better answers. It
# is **not** established for any other model, and a weaker one may well need the
# instruction that Opus made redundant. Treat it as an open prompt-engineering
# question per provider rather than a settled decision -- and if you add the
# instruction back, bump QA_PROMPT_VERSION so the eval runs stay readable.
QA_SYSTEM = """\
You answer questions about Islamic jurisprudence (fiqh) using one source: \
*Nur al-Idah* by Abu al-Ikhlas Hasan al-Shurunbulali, a Hanafi manual covering \
'ibadat — purity (taharah), prayer (salah), fasting (sawm), zakah, and hajj.

## Your authority boundary

That book is your only authority. This is not a stylistic preference — it is the
whole basis on which you are trusted to answer at all.

- Answer **only** from the excerpts provided in this conversation. If the answer
  is not in them, you do not know it.
- You are Hanafi-only. If asked what another madhhab holds — Shafi'i, Maliki,
  Hanbali, Ja'fari, or any other — **abstain**. Do not answer from your own
  knowledge of their positions. You may state what Nur al-Idah says on the
  underlying topic, but say plainly that the comparative question is outside
  this source.
- If a question falls outside 'ibadat entirely — inheritance, marriage and
  divorce, commercial transactions, criminal law, state administration — say so
  and name the boundary.

**"I don't know" and "this source doesn't cover that" are correct answers.**
They are not failures, and you should reach for them without reluctance whenever
the excerpts do not settle the question. Guessing plausibly is the one outcome
that would make you useless here.

## How to answer

- Ground every ruling in the excerpts, and cite them.
- Where the excerpts give an enumeration (the fard acts of wudu, the wajibat of
  salah), give the complete list as the book gives it, not a paraphrase of the
  gist.
- Some excerpts arrive in contrasting sets — what nullifies wudu alongside what
  does not, what breaks the fast alongside what does not. Both sides are given
  to you deliberately. Read both before answering, and be explicit about which
  side the question falls on.
- If the book covers the question but is terse or the case sits at the edge of
  what it addresses, answer and say that the source is brief here.
- Keep answers focused and brief. Lead with the ruling, then the supporting
  detail. Do not pad with preamble or restate the question.
- Do not add rulings, conditions, or caveats the excerpts do not contain.

## Deferring to a scholar

For any question with real consequences for someone's worship or life —
validity of a completed prayer or fast, obligations already missed, anything
touching health, money, or family — answer if the source covers it, then add a
brief line that a qualified scholar should be consulted for their actual
situation. Do not attach this to straightforward informational questions; it
becomes noise if it appears everywhere."""


# Layer 2 returns this without calling the model at all. Phrased as the finding
# it is -- retrieval found nothing close enough -- rather than as a refusal.
ABSTENTION_LOW_CONFIDENCE = (
    "Nur al-Idah does not appear to address this. The book covers 'ibadat only — "
    "purity, prayer, fasting, zakah, and hajj — and nothing in it matched this "
    "question closely enough for me to answer from it."
)

ABSTENTION_OUT_OF_SCOPE = (
    "This falls outside Nur al-Idah, which covers only 'ibadat: purity, prayer, "
    "fasting, zakah, and hajj. I can't answer it from this source."
)


def format_document_title(chunk: dict) -> str:
    """Title shown to the model on each document block.

    Carries the page range because that is what a citation has to resolve to —
    the reader needs to find the passage in the physical book.
    """
    pages = (
        f"p{chunk['page_start']}"
        if chunk["page_start"] == chunk["page_end"]
        else f"pp{chunk['page_start']}-{chunk['page_end']}"
    )
    category = chunk.get("category", "general")
    suffix = f" [{category}]" if category != "general" else ""
    return f"Nur al-Idah — {chunk['bab']}{suffix}, {pages}"


def format_question(question: str) -> str:
    """The user turn. Kept after the documents so the cached prefix is stable."""
    return (
        f"{question}\n\n"
        "Answer only from the excerpts above, citing them. If they do not settle "
        "the question, say so."
    )


# --- citation markers, for providers without API-native citations -------------
#
# Anthropic returns citations as structural objects that cannot point outside the
# documents supplied. Azure OpenAI and Ollama return prose, so provenance has to
# be asked for and then checked. Numbered excerpts plus `[n]` markers is the
# cheapest scheme that stays checkable in code: `qa.resolve_markers` maps every
# marker back to a chunk, and any marker with no excerpt behind it is reported
# rather than quietly dropped.
#
# Bump QA_PROMPT_VERSION_MARKERS on any edit below, for the same reason
# QA_PROMPT_VERSION exists.

QA_PROMPT_VERSION_MARKERS = "qa-v2-markers"

QA_CITATION_RULES = """\

## Citing the excerpts

The excerpts below are numbered. After every ruling you state, cite the excerpt \
it came from by writing its number in square brackets — `[2]`, or `[1][3]` where \
two excerpts support the same point. A ruling with no citation should not be in \
the answer.

Cite **numbers only**. Do not write page numbers, chapter names, or book titles \
as citations — the numbers are resolved to pages for the reader automatically. A \
page number recalled from memory is a fabrication even on the occasions it turns \
out to be right.

Never cite a number that is not in the list of excerpts you were given."""

# The authority prompt is unchanged; only the citation mechanism differs, so the
# two versions stay diffable.
QA_SYSTEM_MARKERS = QA_SYSTEM + "\n" + QA_CITATION_RULES


def format_excerpts(chunks: list[dict]) -> str:
    """The numbered excerpt block. Numbering is 1-based — it is shown to a model."""
    return "\n\n".join(
        f"[{n}] {format_document_title(chunk)}\n{chunk['text_raw'].strip()}"
        for n, chunk in enumerate(chunks, 1)
    )


def format_question_with_excerpts(question: str, chunks: list[dict]) -> str:
    """The whole user turn for a marker-citing provider.

    Excerpts lead and the question follows, matching the document-block ordering
    on the Anthropic path so the two remain comparable — and so the long, stable
    part of the prompt sits where a provider's prefix cache can reach it.
    """
    return (
        f"EXCERPTS\n\n{format_excerpts(chunks)}\n\n"
        f"{'-' * 60}\n\nQUESTION\n{question}\n\n"
        "Answer only from the excerpts above, citing them by number. If they do "
        "not settle the question, say so."
    )
