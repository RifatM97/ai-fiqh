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


# Appended to the user turn, never the system prompt, when retrieval scored in the
# low-confidence band (§1.7 layer 2). It belongs on the user turn for a mechanical
# reason: the system prompt has to stay byte-stable for a provider's prefix cache
# to hit it, and this text varies per question.
LOW_CONFIDENCE_HINT = (
    "\n\nNote: retrieval matched this question only weakly, so the excerpts above "
    "may be about a related matter rather than this one. Read them before relying "
    "on them, and if they do not actually settle the question, say so plainly "
    "rather than stretching them to fit."
)


def format_question_with_excerpts(
    question: str, chunks: list[dict], *, low_confidence: bool = False
) -> str:
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
        + (LOW_CONFIDENCE_HINT if low_confidence else "")
    )


# --- query rewriting, for the grey band below the confidence gate -------------
#
# Measured 2026-09-03 on two real questions that abstained wrongly:
#
#   "How to do Iqama"                     rerank 0.6484  ABSTAIN
#   "does bleeding from the mouth break wudu?"    0.7383  ABSTAIN (gate 0.74)
#
# In both cases retrieval had already succeeded -- the correct chunk was ranked
# #1 by the reranker for the second one -- and the gate discarded it anyway. What
# the passing rephrasings had in common was naming the *aspect* in the book's own
# vocabulary ("the manner in which iqamah is called out", "bleeding which
# overwhelms the saliva"), which is a thing a model can do and a regex cannot.
#
# This asks for a search query, never an answer. It sees no corpus text.

QUERY_REWRITE_VERSION = "rewrite-v1"

QUERY_REWRITE_SYSTEM = """\
You rewrite a user's question into a better *search query* over one book: \
*Nur al-Idah*, a Hanafi manual of 'ibadat — purity, prayer, fasting, zakah and \
hajj. The search is a hybrid of keyword and semantic matching over short \
passages of the book.

You are **not** answering the question. You are choosing words likely to appear
in the passage that answers it.

- Name the specific aspect being asked about, not the general topic. "How to do
  iqamah" retrieves poorly; "the manner in which the iqamah is called out"
  retrieves the passage that answers it.
- Prefer the vocabulary a classical Hanafi manual would use — `nullifies`,
  `is obligatory`, `wajib`, `sunnah`, `makruh`, `nisab` — over casual phrasing.
- Write one clause. Do not add several alternative phrasings.

**Preserve every qualifier that limits the question's scope.** If it names
another school of law — Shafi'i, Maliki, Hanbali, Ja'fari — that name stays in.
If it asks about something outside worship, such as inheritance or trade, that
subject stays in. Removing those would turn a question the system must decline
into one it would answer, which is the one outcome that would make this harmful.

If you cannot improve on the question, return it unchanged."""


# --- query expansion, for `Retriever.search_many` -----------------------------
#
# Supersedes the single rewrite above for the grey band. The same one model call
# now yields several phrasings, whose retrievals are fused and whose rerank scores
# are maxed -- measured to cut the share of naturally-phrased questions scoring
# below the confidence boundary from 31.2% to 10.4%.
#
# The scope-preservation paragraph is the load-bearing part and was verified, not
# assumed: across the should-abstain questions this fires on, every generated
# variant kept the madhhab name, and layer 3 then declined 7/7.

QUERY_EXPANSION_VERSION = "expansion-v1"
QUERY_VARIANTS = 2

QUERY_EXPANSION_SYSTEM = """\
You rewrite a question into alternative search queries over one book: \
*Nur al-Idah*, a Hanafi manual of 'ibadat — purity, prayer, fasting, zakah and \
hajj. The search is a hybrid of keyword and semantic matching over short \
passages of the book.

You are **not** answering the question. You are producing queries that between
them cover however the passage answering it might be worded.

- Make the variants genuinely different. One should use the classical term
  (`wuḍūʾ`, `nullifies`, `wājib`, `niṣāb`), another plain English ("washing
  before prayer", "breaks", "must", "minimum amount").
- Name the specific aspect asked about, not the general topic. "How to do
  iqamah" retrieves poorly; "the manner in which the iqamah is called out"
  retrieves the passage that answers it.
- One clause each. No preamble, no explanation.

**Preserve every qualifier that limits the question's scope, in every variant.**
If it names another school of law — Shafi'i, Maliki, Hanbali, Ja'fari — that name
stays in all of them. If it asks about a subject outside worship, such as
inheritance or trade, that subject stays in. Dropping those would turn a question
the system must decline into one it would answer, which is the one outcome that
would make this harmful."""
