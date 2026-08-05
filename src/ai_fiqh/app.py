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

import sys
from collections import defaultdict
from pathlib import Path

import streamlit as st

if __package__ in (None, ""):  # `streamlit run` executes this as a script
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_fiqh import qa, revision
from ai_fiqh.index import MIN_RERANK_SCORE, Retriever
from ai_fiqh.normalize import display_title

st.set_page_config(page_title="AI-Fiqh", page_icon="📖", layout="centered")

SOURCE = "*Nur al-Idah* — Hanafi fiqh, ʿibādāt only"


@st.cache_resource(show_spinner="Loading index…")
def get_retriever() -> Retriever:
    """One Retriever for the whole server. Loads embeddings once, not per run."""
    return Retriever(verbose=False)


@st.cache_resource(show_spinner=False)
def get_client():
    qa.load_env()
    import anthropic

    # The SDK retries 429/5xx on its own; 5 rather than the default 2 because a
    # 529 overload is transient and a user clicking a button would rather wait
    # than see it fail.
    return anthropic.Anthropic(max_retries=5)


def guarded(fn, *args, **kwargs):
    """Run a model call, turning API failures into a message instead of a stack.

    Overloads and rate limits are ordinary weather, not bugs, and a traceback in
    the middle of the page tells a student nothing they can act on. Returns None
    on failure so the caller leaves previous output alone.
    """
    import anthropic

    try:
        return fn(*args, **kwargs)
    except anthropic.RateLimitError:
        st.warning("Rate limited. Wait a moment and try again.")
    except anthropic.APIStatusError as exc:
        if exc.status_code == 529:
            st.warning("The API is overloaded right now. Try again in a moment.")
        elif exc.status_code == 400 and "credit balance" in str(exc).lower():
            st.error("The Anthropic account is out of credit.")
        else:
            st.error(f"API error {exc.status_code}. Nothing was generated.")
    except anthropic.APIConnectionError:
        st.error("Could not reach the API. Check the network and try again.")
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
            reason = {
                "low-confidence": (
                    f"Nothing retrieved scored above the confidence gate "
                    f"({MIN_RERANK_SCORE}); the best match was "
                    f"{ans.trace.top_score:.3f} if a search ran. No model call "
                    f"was made — this is §1.7 layer 2, which abstains in code."
                ),
                "refusal": "The model declined the request (§1.7 safety classifier).",
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

    pages = sorted(ans.pages_in_context)
    st.caption(
        f"{len(ans.citations)} citation(s) · grounded in {len(ans.chunks)} passage(s) "
        f"· pages {pages[0]}–{pages[-1]}"
        + (" · whole-section lookup (enumeration)" if ans.enumeration else "")
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
                client=get_client(),
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
                retriever=get_retriever(), client=get_client(),
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
                first["id"], n=4, retriever=r, client=get_client(),
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


def main() -> None:
    st.title("AI-Fiqh")
    st.caption(SOURCE)
    qa_tab, mcq_tab, card_tab = st.tabs(["Ask", "Practice questions", "Flashcards"])
    with qa_tab:
        tab_qa()
    with mcq_tab:
        tab_mcq()
    with card_tab:
        tab_flashcards()
    st.divider()
    st.caption(
        "Grounded in one book and one madhhab. For anything consequential, "
        "ask a qualified scholar."
    )


main()
