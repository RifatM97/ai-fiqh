"""Q&A pipeline — retrieve, gate, answer, verify (docs/research.md §2.1, §1.7).

A straight line, not an agent loop:

    question
      -> retrieve (index.py)
      -> confidence gate      LAYER 2 -- abstains in code, before any model call
      -> context budget       fit the excerpts to the provider's window
      -> model + citations    LAYERS 1 & 3 -- numbered excerpts, authority prompt
      -> citation check       LAYER 4 -- every cited source was actually in context
    Answer

The four layers of §1.7 are deliberately independent, and two of them are code.
Layers 2 and 4 keep working on a day when the model doesn't, which is the whole
reason they aren't prompt instructions.

**Layer 1 changed when the project moved off Anthropic.** It used to be
API-native citations: structural objects that, by construction, could not point
outside the documents supplied. Azure OpenAI and Ollama have no equivalent, so
the excerpts are numbered and the model cites `[n]` — and because a model *can*
write `[9]` when eight excerpts exist, `resolve_markers` checks every marker and
reports the ones with nothing behind them. The guarantee moved from the API into
code, which is where layers 2 and 4 already were.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import llm, prompts
from .index import MIN_RERANK_SCORE, Retriever, SearchTrace
from .normalize import fold
from .schemas import RewrittenQuery

# Answer length. These are short answers; the ceiling exists to bound cost and to
# leave room in a small context window, not because answers approach it.
MAX_TOKENS = 2_000

# Headroom for the parts of the prompt that are not excerpt bodies: excerpt
# titles, separators, the question, and the instruction tail.
CONTEXT_SLACK = 512

# The grey band: retrieval that nearly cleared the gate, where a rephrasing is
# worth one cheap call before giving up. Below this, nothing relevant was found
# and rewriting is throwing money at a question the book does not answer.
#
# 0.60 is chosen against the measured distribution rather than by feel. On the
# golden set the should-abstain median is 0.5586, so most genuinely out-of-scope
# questions sit below the floor and never trigger a call at all.
REWRITE_FLOOR = 0.60
REWRITE_MAX_TOKENS = 200

# §2.2: enumeration questions ("list the fard acts of wudu") need the *whole*
# section, because top-k retrieval has no notion of completeness -- it returns
# the k best-matching chunks, which may be a partial list. A cheap heuristic,
# not a model call; guessing wrong just falls back to ordinary search.
_ENUMERATION_CUES = re.compile(
    r"\b(list|enumerate|how many|what are the|which are the|all the|name the)\b",
    re.IGNORECASE,
)
_CATEGORY_CUES = re.compile(
    r"\b(fard|fara.?id|wajib|wajibat|sunan|sunnah|adab|etiquette|makruh|"
    r"disliked|condition|prerequisite|pillar|arkan|nullif|break|invalidat)",
    re.IGNORECASE,
)

# Page references the model might write in prose: "p37", "pp. 17-18", "page 109".
_PAGE_MENTION = re.compile(r"\b(?:pp?\.?|pages?)\s*(\d{1,3})(?:\s*[-–]\s*(\d{1,3}))?\b",
                           re.IGNORECASE)

# A citation marker in the answer text. Two digits is deliberate: excerpt counts
# are single-digit-to-low-teens, and allowing three would start matching years.
_MARKER = re.compile(r"\[(\d{1,2})\]")

# Sentence boundary, for lifting the claim a marker is attached to.
_SENT_END = re.compile(r"[.!?](?:\s|$)")


@dataclass
class Citation:
    """One citation, resolved back to the chunk it points into."""

    cited_text: str
    document_index: int
    document_title: str
    chunk_id: str
    page_start: int
    page_end: int

    def __repr__(self) -> str:
        snippet = self.cited_text[:60].replace("\n", " ")
        return f"<cite p{self.page_start}-{self.page_end} {self.chunk_id}: {snippet!r}>"


@dataclass
class Answer:
    question: str
    text: str
    abstained: bool
    abstain_reason: str | None = None  # "low-confidence" | "refusal" | None
    citations: list[Citation] = field(default_factory=list)
    chunks: list[dict] = field(default_factory=list)
    trace: SearchTrace | None = None
    enumeration: bool = False
    unverified_pages: list[int] = field(default_factory=list)
    # Layer 4's second half, and new with marker citations: numbers the model
    # cited that no excerpt stands behind. Empty on the Anthropic path, where the
    # API makes it impossible.
    unresolved_markers: list[int] = field(default_factory=list)
    # Set when the grey-band rewrite fired and improved retrieval. `original_
    # score` is what the user's own wording scored, kept so the eval can measure
    # whether rewriting is earning its call.
    rewritten_query: str | None = None
    original_score: float | None = None
    # Excerpts the context budget could not fit. Nonzero means the model answered
    # from less than retrieval found, which is worth surfacing, not swallowing.
    context_dropped: int = 0
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_version: str = prompts.QA_PROMPT_VERSION
    provider: str = ""
    model: str = ""
    # Which model actually produced the text. Differs from `model` when the
    # primary provider's content filter refused and a fallback answered instead
    # (§ llm.FallbackClient) -- a reader is entitled to know that happened.
    answered_by: str | None = None
    fallback_used: bool = False

    @property
    def pages_in_context(self) -> set[int]:
        return _pages_covered(self.chunks)

    @property
    def citation_ok(self) -> bool:
        """Layer 4 verdict: nothing cited that was not in context."""
        return not self.unverified_pages and not self.unresolved_markers

    def show(self) -> None:
        print(f"Q: {self.question}\n")
        print(self.text)
        if self.abstained:
            print(f"\n[ABSTAINED — {self.abstain_reason}]")
            if self.rewritten_query and self.trace:
                print(f"  (a rewrite was tried and still missed the gate: "
                      f"{self.rewritten_query!r}, {self.original_score:.4f} -> "
                      f"{self.trace.top_score:.4f})")
            elif self.trace:
                print(f"  (top rerank score {self.trace.top_score:.4f})")
            return
        print(f"\n--- {len(self.citations)} citation(s) ---")
        for c in self.citations:
            print(f"  {c!r}")
        if self.unverified_pages:
            print(f"\n!! LAYER 4 WARNING: cited page(s) not in context: "
                  f"{self.unverified_pages}")
        if self.unresolved_markers:
            print(f"\n!! LAYER 4 WARNING: cited excerpt(s) that do not exist: "
                  f"{self.unresolved_markers}")
        if self.context_dropped:
            print(f"\n!! {self.context_dropped} retrieved chunk(s) did not fit the "
                  f"context window")
        if self.rewritten_query:
            after = f" -> {self.trace.top_score:.4f}" if self.trace else ""
            print(f"\nretrieval used a rewritten query: {self.rewritten_query!r}"
                  f"  (score {self.original_score:.4f}{after})")
        if self.fallback_used:
            print(f"\n!! answered by {self.answered_by} — the primary provider's "
                  f"content filter refused this prompt")
        print(f"\ncontext: {len(self.chunks)} chunks, pages "
              f"{sorted(self.pages_in_context)}")


def _pages_covered(chunks: list[dict]) -> set[int]:
    pages: set[int] = set()
    for c in chunks:
        pages.update(range(c["page_start"], c["page_end"] + 1))
    return pages


def is_enumeration_question(question: str) -> bool:
    """True when the question asks for a complete list rather than a ruling."""
    return bool(_ENUMERATION_CUES.search(question) and _CATEGORY_CUES.search(question))


# --- context budgeting --------------------------------------------------------


def _max_tokens(client: Any) -> int:
    """The answer ceiling, plus whatever the provider spends on thinking first."""
    return MAX_TOKENS + client.thinking_reserve


def fit_to_context(
    chunks: list[dict], client: Any, system: str, question: str
) -> tuple[list[dict], int]:
    """Trim the excerpt set to what the provider's context window can hold.

    A no-op on a 128k Azure deployment and on Anthropic. It exists for small
    local windows: gemma2 carries 8,192 tokens, of which the authority prompt
    takes ~750 and the answer reserve another 2,000, leaving roughly 4,800 for
    excerpts. Ordinary retrieval needs at most ~3,200 of that, so this only ever
    bites on the §2.2 enumeration path, where a whole section is merged in and
    the Hajj rituals chapter alone is ~5,600 tokens.

    **Trimming from the tail is the whole design.** `chunks` arrives in priority
    order in both modes — reranked-best-first for an ordinary question, and
    whole-section-first for an enumeration — so dropping the tail drops the least
    load-bearing context in either case, and never separates the top hit from the
    §1.3 polarity siblings that follow it. At least one chunk always survives.
    """
    budget = (
        client.context_tokens
        - llm.estimate_tokens(system)
        - llm.estimate_tokens(question)
        - _max_tokens(client)
        - CONTEXT_SLACK
    )
    kept: list[dict] = []
    used = 0
    for chunk in chunks:
        cost = llm.estimate_tokens(chunk["text_raw"]) + llm.estimate_tokens(
            prompts.format_document_title(chunk)
        )
        if kept and used + cost > budget:
            break
        kept.append(chunk)
        used += cost
    return kept, len(chunks) - len(kept)


# --- citations ----------------------------------------------------------------


def _build_documents(chunks: list[dict]) -> list[dict]:
    """Anthropic-only: document blocks with API-native citations enabled.

    Kept so the original layer-1 implementation stays runnable for comparison
    against the marker scheme. Note the constraint that shaped §2.3: this is
    incompatible with structured output, so revision mode makes the opposite
    trade.
    """
    return [
        {
            "type": "document",
            "source": {
                "type": "text",
                "media_type": "text/plain",
                "data": c["text_raw"],
            },
            "title": prompts.format_document_title(c),
            "citations": {"enabled": True},
        }
        for c in chunks
    ]


def _sentence_around(text: str, pos: int) -> str:
    """The sentence a marker sits in — what the citation is being attached to.

    A native citation carries the *source* span it quotes. A marker cannot: the
    model wrote a number, not a quote. So this carries the claim instead, which
    is the more useful half for a reader checking an answer — they can see what
    was asserted and open the cited page to confirm it.
    """
    start = 0
    for m in _SENT_END.finditer(text, 0, pos):
        start = m.end()
    end_match = _SENT_END.search(text, pos)
    end = end_match.end() if end_match else len(text)
    return re.sub(r"\s+", " ", text[start:end]).strip(" -*•\t")


def resolve_markers(
    text: str, chunks: list[dict]
) -> tuple[list[Citation], list[int]]:
    """Layer 1 for providers without API-native citations.

    Returns the resolved citations and the markers that resolved to nothing. The
    second half is the part that matters: an API citation object cannot name a
    document that was not supplied, but a model writing prose absolutely can
    write `[9]` over five excerpts, and silently discarding those would hide a
    fabrication that this check makes visible.
    """
    citations: list[Citation] = []
    unresolved: list[int] = []
    seen: set[tuple[str, str]] = set()

    for match in _MARKER.finditer(text):
        n = int(match.group(1))
        if not 1 <= n <= len(chunks):
            if n not in unresolved:
                unresolved.append(n)
            continue
        chunk = chunks[n - 1]
        claim = _sentence_around(text, match.start())
        key = (chunk["id"], claim)
        if key in seen:
            continue
        seen.add(key)
        citations.append(
            Citation(
                cited_text=claim,
                document_index=n - 1,
                document_title=prompts.format_document_title(chunk),
                chunk_id=chunk["id"],
                page_start=chunk["page_start"],
                page_end=chunk["page_end"],
            )
        )
    return citations, sorted(unresolved)


def verify_citations(text: str, chunks: list[dict]) -> list[int]:
    """Layer 4 of §1.7 — page numbers written in prose that weren't in context.

    Resolved citations need no checking: on Anthropic they cannot point outside
    the supplied documents, and on every other provider `resolve_markers` has
    already rejected the ones that do. What *can* still drift is the model
    writing "p61" into the answer text from memory. Any page named in prose that
    no supplied chunk covers is a hallucination, and detectable without a human.
    """
    available = _pages_covered(chunks)
    claimed: set[int] = set()
    for match in _PAGE_MENTION.finditer(text):
        start = int(match.group(1))
        end = int(match.group(2)) if match.group(2) else start
        if end < start or end - start > 20:  # a range that wide isn't a citation
            claimed.add(start)
            continue
        claimed.update(range(start, end + 1))
    return sorted(claimed - available)


# --- query rewriting ----------------------------------------------------------


def rewrite_query(question: str, client: Any) -> str | None:
    """A better *search query* for a question that nearly cleared the gate.

    Returns None when the model declines to improve it, returns it unchanged, or
    fails outright — a failed rewrite must degrade to the original abstention,
    never to an exception, because this runs on the path of a question that was
    about to be answered with "the book does not cover this".

    **This does not weaken layer 2.** The abstention decision is still made in
    code, on a reranker score, after this returns; all this changes is which
    passages that score is computed over. The model sees the question and
    nothing else — no corpus text, and its output never reaches the user.
    """
    try:
        result = client.parse(
            prompts.QUERY_REWRITE_SYSTEM,
            question,
            RewrittenQuery,
            max_tokens=REWRITE_MAX_TOKENS + client.thinking_reserve,
        )
    except llm.LLMError:
        return None
    candidate = result.query.strip()
    if not candidate or fold(candidate) == fold(question):
        return None
    return candidate


# --- the pipeline -------------------------------------------------------------


def _ask(
    client: Any, chunks: list[dict], question: str
) -> tuple[llm.Completion, list[Citation], list[int]]:
    """One model call, returning text plus provenance however the provider gives it."""
    max_tokens = _max_tokens(client)

    if client.supports_native_citations:
        completion, raw = client.cited_complete(
            prompts.QA_SYSTEM,
            _build_documents(chunks),
            prompts.format_question(question),
            max_tokens=max_tokens,
        )
        citations = [
            Citation(
                cited_text=quote,
                document_index=idx,
                document_title=prompts.format_document_title(chunks[idx]),
                chunk_id=chunks[idx]["id"],
                page_start=chunks[idx]["page_start"],
                page_end=chunks[idx]["page_end"],
            )
            for idx, quote in raw
            if 0 <= idx < len(chunks)
        ]
        return completion, citations, []

    completion = client.complete(
        prompts.QA_SYSTEM_MARKERS,
        prompts.format_question_with_excerpts(question, chunks),
        max_tokens=max_tokens,
    )
    citations, unresolved = resolve_markers(completion.text, chunks)
    return completion, citations, unresolved


def answer(
    question: str,
    *,
    retriever: Retriever | None = None,
    client: Any = None,
    gate: float = MIN_RERANK_SCORE,
    rewrite: bool = True,
) -> Answer:
    """Answer one question, or abstain.

    `gate` is exposed so the eval harness can sweep the §1.7 layer-2 threshold
    against false-abstention rate without editing module state. `rewrite` is
    exposed for the same reason — so the grey-band retry can be measured against
    its own absence rather than assumed to help.
    """
    r = retriever if retriever is not None else Retriever(verbose=False)
    trace = r.search(question)

    # --- Grey band: one rephrasing before giving up (§1.5) -------------------
    # Retrieval frequently succeeds while the *score* misses. Measured
    # 2026-09-03: "does bleeding from the mouth break wudu?" put the exactly
    # correct chunk at rerank #1 and still abstained, by 0.0017.
    rewritten_query: str | None = None
    original_score: float | None = None
    if rewrite and REWRITE_FLOOR <= trace.top_score < gate:
        if client is None:
            client = llm.get_client()
        candidate = rewrite_query(question, client)
        if candidate:
            retried = r.search(candidate)
            # Keep the rephrasing only if it actually retrieved better. A rewrite
            # that scores worse is discarded silently -- the user asked the first
            # question, and there is no reason to answer a worse one.
            if retried.top_score > trace.top_score:
                original_score, rewritten_query, trace = (
                    trace.top_score, candidate, retried
                )

    # --- Layer 2: abstain in code, before any answer is generated ------------
    if trace.top_score < gate:
        return Answer(
            question=question,
            text=prompts.ABSTENTION_LOW_CONFIDENCE,
            abstained=True,
            abstain_reason="low-confidence",
            trace=trace,
            chunks=[],
            rewritten_query=rewritten_query,
            original_score=original_score,
        )

    chunks = [s.chunk for s in trace.results]

    enumeration = is_enumeration_question(question)
    if enumeration and trace.reranked:
        # §2.2 -- ground the answer in the complete section, not a similarity-
        # ranked slice of it. Merged rather than substituted so the group
        # expansion from §1.3 survives. Section first, because `fit_to_context`
        # trims from the tail and the section is what this mode is for.
        section = r.get_section(trace.reranked[0].id)
        section_ids = {s["id"] for s in section}
        chunks = section + [c for c in chunks if c["id"] not in section_ids]

    if client is None:
        client = llm.get_client()

    system = (
        prompts.QA_SYSTEM
        if client.supports_native_citations
        else prompts.QA_SYSTEM_MARKERS
    )
    prompt_version = (
        prompts.QA_PROMPT_VERSION
        if client.supports_native_citations
        else prompts.QA_PROMPT_VERSION_MARKERS
    )
    chunks, dropped = fit_to_context(chunks, client, system, question)

    completion, citations, unresolved = _ask(client, chunks, question)

    # A provider may decline outright — an Anthropic refusal stop reason, or an
    # Azure content filter. Check before reading the (empty) content.
    if completion.refused:
        return Answer(
            question=question,
            text=prompts.ABSTENTION_OUT_OF_SCOPE,
            abstained=True,
            abstain_reason="refusal",
            chunks=chunks,
            trace=trace,
            enumeration=enumeration,
            context_dropped=dropped,
            rewritten_query=rewritten_query,
            original_score=original_score,
            stop_reason=completion.stop_reason,
            prompt_version=prompt_version,
            provider=client.provider,
            model=client.model,
        )

    return Answer(
        question=question,
        text=completion.text,
        abstained=False,
        citations=citations,
        chunks=chunks,
        trace=trace,
        enumeration=enumeration,
        unverified_pages=verify_citations(completion.text, chunks),  # Layer 4
        unresolved_markers=unresolved,
        context_dropped=dropped,
        answered_by=completion.answered_by or llm.describe(client),
        fallback_used=completion.fallback_used,
        rewritten_query=rewritten_query,
        original_score=original_score,
        stop_reason=completion.stop_reason,
        usage=completion.usage,
        prompt_version=prompt_version,
        provider=client.provider,
        model=client.model,
    )


def main() -> None:
    """Smoke-test one question of each kind."""
    r = Retriever(verbose=False)
    client = llm.get_client()
    print(f"provider: {llm.describe(client)}  (context {client.context_tokens:,} tok)\n")
    for q in (
        "What are the four fard acts of wudu?",
        "Does laughing aloud break wudu?",
        "How is inheritance divided among sons and daughters?",
        "Do Shafi'i scholars consider bleeding to break wudu?",
    ):
        print("=" * 72)
        answer(q, retriever=r, client=client).show()
        print()


if __name__ == "__main__":
    main()
