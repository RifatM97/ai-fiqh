"""Streamlit interface (docs/research.md §2.1).

Two tabs, and the mode is whichever tab you are on. That is the whole
orchestration layer: §2.1 chose user-selected modes over a router because at two
modes a menu is cheaper and more predictable than a classifier, and neither
pipeline needs an agent loop once the mode is known.

So this file holds no retrieval logic, no prompt, and no validation. It calls
`qa.answer`, `revision.generate_mcqs` and `revision.build_deck`, and its real
work is showing the things those return that a user must not miss: whether the
system abstained and why, which pages an answer rests on, and whether layer 4
caught a page the model named that was never in context.

    uv run streamlit run src/ai_fiqh/app.py
"""

from __future__ import annotations

import logging
import os
import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

if __package__ in (None, ""):  # `streamlit run` executes this as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_fiqh import llm, qa, revision
from ai_fiqh.index import ABSTAIN_BELOW, MIN_RERANK_SCORE, Retriever
from ai_fiqh.normalize import display_title

# Streamlit renders results to the browser, so without this the server's stdout
# says nothing about retrieval, gate decisions or model calls — which is exactly
# what a deployed log stream can show. `force` because Streamlit configures
# logging first. AI_FIQH_LOG_LEVEL=DEBUG also logs question text (see qa.py).
logging.basicConfig(
    level=os.environ.get("AI_FIQH_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    stream=sys.stdout,
    force=True,
)
# Two HTTP lines per model call (the Azure dialect probe makes it three) would
# bury the pipeline's own lines in the deployed stream.
for _noisy in ("httpx", "httpx2", "httpcore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

st.set_page_config(page_title="AI-Fiqh", page_icon="📖", layout="centered")

SOURCE = "*Nur al-Idah* — Hanafi fiqh, ʿibādāt only"


@st.cache_resource(show_spinner="Loading index…")
def get_retriever() -> Retriever:
    """One Retriever for the whole server. Loads embeddings once, not per run."""
    return Retriever(verbose=False)


@st.cache_resource(show_spinner=False)
def get_client():
    """One model client for the whole server. Raises only on misconfiguration."""
    return llm.get_client()


def require_client():
    """The client, or a stopped page with an actionable message.

    A missing endpoint or deployment name is not something a spinner should hide
    and not something a traceback explains, so it ends the run with the name of
    the setting that is absent.
    """
    try:
        return get_client()
    except llm.LLMError as exc:
        st.error(str(exc))
        st.stop()


def guarded(fn, *args, **kwargs):
    """Run a model call, turning provider failures into a message instead of a stack.

    Overloads and rate limits are ordinary weather, not bugs, and a traceback in
    the middle of the page tells a student nothing they can act on. Returns None
    on failure so the caller leaves previous output alone. Every provider raises
    into the same four classes, so this reads the same whichever one is selected.
    """
    try:
        return fn(*args, **kwargs)
    except llm.LLMOverloaded as exc:
        st.warning(f"{exc} Wait a moment and try again.")
    except llm.LLMUnavailable as exc:
        st.error(str(exc))
    except llm.LLMConfigError as exc:
        st.error(f"Configuration problem — {exc}")
    except llm.LLMError as exc:
        st.error(f"The model call failed: {exc}")
    return None


@st.cache_data(show_spinner=False)
def kitab_sections(kitab: str) -> list[tuple[str, str]]:
    """(raw bab, display name) pairs, ordered by page.

    The raw title stays the key -- `get_section` matches on it exactly, glyph
    and all -- while the cleaned one is what the picker shows.
    """
    r = get_retriever()
    first: dict[str, int] = {}
    for c in r.chunks:
        if c["kitab"] == kitab:
            first.setdefault(c["bab"], c["page_start"])
    return [(b, display_title(b)) for b in sorted(first, key=lambda b: first[b])]


@st.cache_data(show_spinner=False)
def mcq_targets() -> dict[str, list[str]]:
    """Which (kitab, category) pairs can actually produce a question.

    Only 13 of them can. Zakah has no category-labelled chunks at all and Hajj
    has one, so the MCQ path can build no distractor pool for either -- offering
    them in a dropdown would be offering a button that cannot work.
    """
    import random

    r = get_retriever()
    pool = revision.build_item_pool(r.chunks)
    rng = random.Random(0)
    out: dict[str, list[str]] = defaultdict(list)
    for kitab in sorted({i.kitab for v in pool.values() for i in v}):
        for category in sorted(pool):
            if revision.select_items(pool, kitab, category, rng) is not None:
                out[kitab].append(category)
    return dict(out)


# --- Q&A ---------------------------------------------------------------------


def render_answer(ans: qa.Answer) -> None:
    if ans.abstained:
        # Abstention is a correct outcome, not an error, and it must not read as
        # a failed request -- the whole design exists to make it happen.
        st.info(ans.text)
        with st.expander("Why it abstained"):
            retried = (
                f" A rewritten query ({ans.rewritten_query!r}) was tried and "
                f"still missed, {ans.original_score:.3f} → "
                f"{ans.trace.top_score:.3f}."
                if ans.rewritten_query and ans.trace else ""
            )
            reason = {
                "low-confidence": (
                    f"Nothing retrieved scored above {ABSTAIN_BELOW}, the level "
                    f"below which the score genuinely does discriminate; the best "
                    f"match was {ans.trace.top_score:.3f} if a search ran. The "
                    f"abstention itself is §1.7 layer 2, decided in code." + retried
                ),
                "refusal": (
                    "The provider declined or filtered the request "
                    "(§1.7 — a safety classifier or content filter)."
                ),
            }.get(ans.abstain_reason, ans.abstain_reason or "unknown")
            st.write(reason)
        return

    st.markdown(ans.text)

    if ans.unverified_pages:
        # Layer 4. A page named in prose that no supplied chunk covers is a
        # hallucination, and the user is the one who needs to know.
        st.error(
            f"**Citation check failed.** The answer names page(s) "
            f"{ans.unverified_pages}, which were not in the retrieved context. "
            f"Treat this answer as unreliable."
        )

    if ans.unresolved_markers:
        # The marker-scheme half of layer 4, and the reason the scheme is
        # acceptable at all: a cited excerpt that does not exist is caught here
        # rather than read as provenance.
        st.error(
            f"**Citation check failed.** The answer cites excerpt(s) "
            f"{ans.unresolved_markers}, which were never supplied to it. Treat "
            f"this answer as unreliable."
        )

    if ans.context_dropped:
        st.warning(
            f"{ans.context_dropped} retrieved passage(s) did not fit the model's "
            f"context window and were not sent. The answer may be incomplete."
        )

    pages = sorted(ans.pages_in_context)
    st.caption(
        f"{len(ans.citations)} citation(s) · grounded in {len(ans.chunks)} passage(s) "
        f"· pages {pages[0]}–{pages[-1]}"
        + (" · whole-section lookup (enumeration)" if ans.enumeration else "")
    )

    if ans.low_confidence:
        # Answered rather than abstained, but on weak retrieval. The reader is the
        # one who can judge whether the cited passages are really about their
        # question, so they have to be told that is in doubt.
        st.warning(
            f"**Low confidence.** Retrieval matched this question only weakly "
            f"(best passage scored {ans.trace.top_score:.3f} against a "
            f"{MIN_RERANK_SCORE} confidence boundary). The answer was generated "
            f"anyway rather than withheld, but check the cited passages below are "
            f"really about what you asked."
            if ans.trace else "**Low confidence.** Retrieval matched only weakly."
        )

    if ans.fallback_used:
        # Not a detail. The answer came from a different, weaker model than the
        # one the footer names, and the reason is that the primary provider
        # refused to process a passage of the source book.
        st.warning(
            f"The configured model's content filter refused this passage of the "
            f"book, so this answer was generated by **{ans.answered_by}** "
            f"instead. The retrieved sources and all citation checks are "
            f"unchanged."
        )

    if ans.rewritten_query:
        # The user asked one thing and the book was searched for another. That is
        # a retrieval-only substitution and it changed nothing about what may be
        # asserted, but it is not something to hide from them.
        st.caption(
            f"🔎 Searched the book for *“{ans.rewritten_query}”* — the original "
            f"wording scored {ans.original_score:.3f} against the "
            f"{MIN_RERANK_SCORE} confidence gate."
        )

    if ans.citations:
        bab_of = {c["id"]: c["bab"] for c in ans.chunks}
        with st.expander(f"Cited passages ({len(ans.citations)})"):
            for c in ans.citations:
                st.markdown(
                    f"**p{c.page_start}–{c.page_end}** · "
                    f"{display_title(bab_of.get(c.chunk_id, ''))}"
                )
                st.caption(f"> {c.cited_text.strip()}")

    with st.expander("Retrieval trace"):
        if ans.trace:
            st.caption(
                f"gate {MIN_RERANK_SCORE} · top reranked score "
                f"{ans.trace.top_score:.3f}"
                + (f" · rewritten from {ans.original_score:.3f}"
                   if ans.rewritten_query else "")
            )
            for s in ans.trace.results:
                mark = " ← group expansion" if s.source == "group-expansion" else ""
                st.text(
                    f"{s.rank:>2}. {s.score:+.4f}  p{s.chunk['page_start']}–"
                    f"{s.chunk['page_end']}  {display_title(s.chunk['bab'])}{mark}"
                )


def tab_qa() -> None:
    st.caption(
        "Answers come only from the book. Questions it does not cover — other "
        "madhhabs, anything outside ʿibādāt — are declined rather than guessed."
    )
    with st.form("qa"):
        question = st.text_input(
            "Question",
            placeholder="Does laughing aloud break wuḍūʾ?",
            label_visibility="collapsed",
        )
        asked = st.form_submit_button("Ask", type="primary")

    if asked and question.strip():
        with st.spinner("Retrieving and answering…"):
            ans = guarded(
                qa.answer,
                question.strip(),
                retriever=get_retriever(),
                client=require_client(),
            )
        if ans is not None:
            st.session_state["last_answer"] = ans

    if (ans := st.session_state.get("last_answer")) is not None:
        st.divider()
        render_answer(ans)


# --- Revision ----------------------------------------------------------------


def tab_mcq() -> None:
    targets = mcq_targets()
    st.caption(
        "Wrong answers are drawn from the book's own categories, so a distractor "
        "is wrong because the book files it elsewhere — not because the model "
        "judged it wrong."
    )

    col1, col2, col3 = st.columns([2, 2, 1])
    kitab = col1.selectbox("Book", sorted(targets), format_func=str.title)
    category = col2.selectbox("Category tested", targets[kitab])
    count = col3.number_input("How many", 1, 5, 2)

    if st.button("Generate questions", type="primary"):
        with st.spinner(f"Writing {count} question(s)…"):
            got = guarded(
                revision.generate_mcqs,
                kitab, category, n=int(count),
                retriever=get_retriever(), client=require_client(),
            )
        if got is not None:
            st.session_state["mcqs"] = got

    mcqs, fails = st.session_state.get("mcqs", ([], []))
    for n, m in enumerate(mcqs):
        st.divider()
        st.markdown(f"**{n + 1}. {m.question}**")
        choice = st.radio(
            "options", m.options, index=None,
            key=f"mcq-{n}-{hash(m.question)}", label_visibility="collapsed",
        )
        if choice is not None:
            if m.options.index(choice) == m.correct_index:
                st.success("Correct.")
            else:
                st.error(f"Not quite — the answer is **{m.options[m.correct_index]}**.")
            st.caption(f"{m.explanation}")
            st.caption(
                f"p{m.source_page} · {display_title(m.bab)} · "
                f"distractors drawn from: {', '.join(m.distractor_categories)}"
            )
    if fails:
        with st.expander(f"{len(fails)} generation(s) discarded by validation"):
            for f in fails:
                st.text(f"[{f.check}] {f.detail}")


def tab_flashcards() -> None:
    st.caption(
        "Flashcards need no distractor pool, which is why they are the only "
        "revision route for Zakah and Hajj — neither has the category metadata "
        "an MCQ needs."
    )
    kitabs = ["taharah", "salah", "sawm", "zakah", "hajj"]
    col1, col2 = st.columns([2, 3])
    kitab = col1.selectbox("Book", kitabs, format_func=str.title, key="fc-kitab")
    sections = kitab_sections(kitab)
    chosen = col2.selectbox(
        "Section", sections, format_func=lambda pair: pair[1], key="fc-section"
    )

    if st.button("Make cards", type="primary"):
        r = get_retriever()
        first = next(c for c in r.chunks
                     if c["kitab"] == kitab and c["bab"] == chosen[0])
        with st.spinner("Writing cards…"):
            got = guarded(
                revision.generate_flashcards,
                first["id"], n=4, retriever=r, client=require_client(),
            )
        if got is not None:
            st.session_state["cards"] = got

    cards, fails = st.session_state.get("cards", ([], []))
    for n, c in enumerate(cards):
        with st.expander(f"{n + 1}.  {c.front}"):
            st.markdown(c.back)
            st.caption(f"p{c.source_page} · {display_title(c.bab)}")
    if fails:
        with st.expander(f"{len(fails)} card(s) discarded by validation"):
            for f in fails:
                st.text(f"[{f.check}] {f.detail}")


def signed_in_line() -> None:
    """Who the platform signed in, and a way out.

    Container Apps authentication handles the sign-in before any request
    reaches this process and passes the identity as request headers. Locally
    there is no such layer and nothing renders. The name is shown to its own
    owner only and never logged.
    """
    name = st.context.headers.get("X-MS-CLIENT-PRINCIPAL-NAME")
    if name:
        st.caption(
            f"Signed in as {name} · "
            "[Sign out](/.auth/logout?post_logout_redirect_uri=/)"
        )


def main() -> None:
    st.title("AI-Fiqh")
    st.caption(SOURCE)
    signed_in_line()
    qa_tab, mcq_tab, card_tab = st.tabs(["Ask", "Practice questions", "Flashcards"])
    with qa_tab:
        tab_qa()
    with mcq_tab:
        tab_mcq()
    with card_tab:
        tab_flashcards()
    st.divider()
    # Which model answered is not a detail here: the abstention behaviour the
    # whole project rests on is model-dependent, so it belongs on the page.
    try:
        backend = llm.describe(get_client())
    except llm.LLMError:
        backend = f"{llm.active_provider()} (not configured)"
    st.caption(
        "Grounded in one book and one madhhab. For anything consequential, "
        f"ask a qualified scholar.  ·  answered by `{backend}`"
    )


main()
