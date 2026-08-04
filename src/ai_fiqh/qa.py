"""Q&A pipeline — retrieve, gate, answer, verify (docs/research.md §2.1, §1.7).

A straight line, not an agent loop:

    question
      -> retrieve (index.py)
      -> confidence gate      LAYER 2 -- abstains in code, before any model call
      -> Claude + citations   LAYERS 1 & 3 -- document blocks, authority prompt
      -> citation check       LAYER 4 -- every cited page was actually in context
    Answer

The four layers of §1.7 are deliberately independent, and two of them are code.
Layers 2 and 4 keep working on a day when the model doesn't, which is the whole
reason they aren't prompt instructions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import prompts
from .index import MIN_RERANK_SCORE, Retriever, SearchTrace, load_env

MODEL = "claude-opus-5"
MAX_TOKENS = 16_000

# Thinking is on by default on Opus 5, and `max_tokens` caps thinking *plus*
# answer text -- hence the generous ceiling above for what are short answers.
# Effort is the cost dial: `high` matches the API default and is where a source
# that must not be misquoted belongs. Sweep it down in eval, not by intuition.
EFFORT = "high"

# Opus 5's safety classifiers can decline a request outright; a fallback re-runs
# it on another model server-side instead of returning the refusal. Unlikely to
# fire on fiqh questions, but it costs nothing when unused. Set False to disable.
ENABLE_REFUSAL_FALLBACK = True
FALLBACK_BETA = "server-side-fallback-2026-07-01"

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


@dataclass
class Citation:
    """One API-native citation, resolved back to the chunk it points into."""

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
    stop_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_version: str = prompts.QA_PROMPT_VERSION

    @property
    def pages_in_context(self) -> set[int]:
        return _pages_covered(self.chunks)

    def show(self) -> None:
        print(f"Q: {self.question}\n")
        print(self.text)
        if self.abstained:
            print(f"\n[ABSTAINED — {self.abstain_reason}]")
            return
        print(f"\n--- {len(self.citations)} citation(s) ---")
        for c in self.citations:
            print(f"  {c!r}")
        if self.unverified_pages:
            print(f"\n!! LAYER 4 WARNING: cited page(s) not in context: "
                  f"{self.unverified_pages}")
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


def _build_documents(chunks: list[dict]) -> list[dict]:
    """Layer 1 of §1.7 — document blocks with API-native citations enabled.

    Citations come back as structural objects pointing into these blocks, rather
    than as prose the model was asked nicely to produce. Note the constraint:
    this is incompatible with `output_config.format`, so the revision pipeline
    (§2.3) has to make the opposite choice.
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


def _extract(response: Any, chunks: list[dict]) -> tuple[str, list[Citation]]:
    """Pull answer text and resolved citations out of the response."""
    parts: list[str] = []
    citations: list[Citation] = []
    for block in response.content:
        if block.type != "text":
            continue
        parts.append(block.text)
        for raw in getattr(block, "citations", None) or []:
            idx = getattr(raw, "document_index", None)
            if idx is None or not 0 <= idx < len(chunks):
                continue  # cannot happen via the API, but this is a safety layer
            chunk = chunks[idx]
            citations.append(
                Citation(
                    cited_text=getattr(raw, "cited_text", ""),
                    document_index=idx,
                    document_title=getattr(raw, "document_title", "") or "",
                    chunk_id=chunk["id"],
                    page_start=chunk["page_start"],
                    page_end=chunk["page_end"],
                )
            )
    return "".join(parts).strip(), citations


def verify_citations(text: str, chunks: list[dict]) -> list[int]:
    """Layer 4 of §1.7 — page numbers written in prose that weren't in context.

    The API's citation objects cannot point outside the documents we supplied, so
    they need no checking. What *can* drift is the model writing "p61" into the
    answer text from memory. Any page named in prose that no supplied chunk
    covers is a hallucination, and detectable without a human.
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


def _call_model(client: Any, documents: list[dict], question: str) -> Any:
    """One request. Falls back to the non-beta path if the fallback beta is off."""
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "output_config": {"effort": EFFORT},
        "system": [
            {
                "type": "text",
                "text": prompts.QA_SYSTEM,
                # The system prompt is byte-stable across every question, so it
                # caches once and every later call reads it (§ prompt caching).
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [
            {
                "role": "user",
                "content": [
                    *documents,
                    {"type": "text", "text": prompts.format_question(question)},
                ],
            }
        ],
    }
    if not ENABLE_REFUSAL_FALLBACK:
        return client.messages.create(**kwargs)

    import anthropic

    try:
        return client.beta.messages.create(
            betas=[FALLBACK_BETA], fallbacks="default", **kwargs
        )
    except anthropic.BadRequestError as exc:
        if "fallback" not in str(exc).lower():
            raise
        return client.messages.create(**kwargs)  # beta not enabled on this key


def answer(
    question: str,
    *,
    retriever: Retriever | None = None,
    client: Any = None,
    gate: float = MIN_RERANK_SCORE,
) -> Answer:
    """Answer one question, or abstain.

    `gate` is exposed so the eval harness can sweep the §1.7 layer-2 threshold
    against false-abstention rate without editing module state.
    """
    r = retriever if retriever is not None else Retriever(verbose=False)
    trace = r.search(question)

    # --- Layer 2: abstain in code, before the model is ever called -----------
    if trace.top_score < gate:
        return Answer(
            question=question,
            text=prompts.ABSTENTION_LOW_CONFIDENCE,
            abstained=True,
            abstain_reason="low-confidence",
            trace=trace,
            chunks=[],
        )

    chunks = [s.chunk for s in trace.results]

    enumeration = is_enumeration_question(question)
    if enumeration and trace.reranked:
        # §2.2 -- ground the answer in the complete section, not a similarity-
        # ranked slice of it. Merged rather than substituted so the group
        # expansion from §1.3 survives.
        section = r.get_section(trace.reranked[0].id)
        section_ids = {s["id"] for s in section}
        chunks = section + [c for c in chunks if c["id"] not in section_ids]

    if client is None:
        load_env()
        import anthropic

        client = anthropic.Anthropic()

    response = _call_model(client, _build_documents(chunks), question)

    # Opus 5 can decline outright — check before reading content.
    if response.stop_reason == "refusal":
        return Answer(
            question=question,
            text=prompts.ABSTENTION_OUT_OF_SCOPE,
            abstained=True,
            abstain_reason="refusal",
            chunks=chunks,
            trace=trace,
            enumeration=enumeration,
            stop_reason=response.stop_reason,
        )

    text, citations = _extract(response, chunks)
    usage = response.usage

    return Answer(
        question=question,
        text=text,
        abstained=False,
        citations=citations,
        chunks=chunks,
        trace=trace,
        enumeration=enumeration,
        unverified_pages=verify_citations(text, chunks),  # Layer 4
        stop_reason=response.stop_reason,
        usage={
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0),
            "cache_creation_input_tokens": getattr(
                usage, "cache_creation_input_tokens", 0
            ),
        },
    )


def main() -> None:
    """Smoke-test one question of each kind."""
    r = Retriever(verbose=False)
    for q in (
        "What are the four fard acts of wudu?",
        "Does laughing aloud break wudu?",
        "How is inheritance divided among sons and daughters?",
        "Do Shafi'i scholars consider bleeding to break wudu?",
    ):
        print("=" * 72)
        answer(q, retriever=r).show()
        print()


if __name__ == "__main__":
    main()
