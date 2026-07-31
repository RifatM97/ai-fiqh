# AI-Fiqh — Design Research

**Started:** 2026-07-27
**Status:** design complete; ingestion implemented and verified (2026-07-30)

> Sections marked **implemented** are a record of what was built and verified,
> not a plan. Where the data contradicted the original design, the original
> claim is left in place with a **Correction** beneath it — §1.2.1, §1.3, §1.6.

Decisions locked before writing (see [PROJECT_TRACKER.md](PROJECT_TRACKER.md)):
Hanafi, `ibadat` only, Nur al-Idah as sole authority, `uv`.

Decisions locked in this session:

| Question | Answer |
|---|---|
| RAG vs. long-context | **Build RAG properly.** Corpus size is not a reason to skip it — citations, abstention, and the polarity problem are the point. |
| Model hosting | **Claude API for generation, hosted API for embeddings.** |
| Interface | **Jupyter notebook first**, web UI later. |
| Exam-revision mode | **In scope from the start**, designed alongside Q&A. |

---

## 1. RAG architecture

### 1.1 Corpus measurement

Measured directly, not estimated:

| Property | Value |
|---|---|
| Pages | 164 |
| Characters (extracted text layer) | 313,038 |
| Words | 54,652 |
| Tokens (chars/3.6 heuristic) | ~87,000 |
| Tokens (realistic, with combining diacritics) | ~100,000–120,000 |

Combining diacritics (`ḍ`, `ū`, `ʾ`) tokenize badly — each accented character often costs 2–3 tokens instead of one. Treat the heuristic as a floor. **Re-measure with `client.messages.count_tokens()` before sizing anything**; do not trust the character estimate for budget decisions.

The whole book fits in one context window with room to spare. That fact doesn't argue against RAG here — it argues that retrieval quality is cheap to fix, because we can always afford to over-retrieve.

### 1.2 Chunking — structure-aware, driven by the printed ToC

The book has no embedded PDF outline (`get_toc()` returns 0 entries), but pages 2–5 carry a human-readable table of contents with printed page numbers. That's the parsing target.

The corpus's structural regularity is the biggest asset in this project. Every act of worship decomposes into the same legal categories. Chunk on those boundaries, not on token counts:

```
kitab (book)     → Taharah | Salah | Zakat | Sawm | Hajj
  bab (chapter)  → e.g. "Wudu", "The Description of Salah"
    category     → fard | wajib | sunnah | adab | makruh | nullifier | non-nullifier
```

One chunk = one `(kitab, bab, category)` triple. Metadata on every chunk:

```python
{
  "id": "017-those-things-which-nullify-wudu",
  "kitab": "taharah",
  "bab": "Those things which nullify wuḍū’",
  "category": "nullifier",
  "polarity": "affirmative",       # or "negative"
  "group_id": "wudu-nullifiers",   # links contrasting sections — see §1.3
  "page_start": 17,
  "page_end": 18,
  "part": 0, "n_parts": 1,         # sub-split index — see §1.2.1
  "text_raw": "...",               # with diacritics, shown to the model
  "text_folded": "...",            # diacritics stripped, for BM25
}
```

**Why not fixed-size or recursive chunking:** a 512-token window that straddles the boundary between "things which nullify wuḍūʾ" and "things which do not break wuḍūʾ" produces a chunk that is legally incoherent and retrievally toxic. The structure is handing us correct boundaries for free.

### 1.2.1 Correction — structure alone is not sufficient

*Added after implementing ingestion. The original claim, that structural boundaries were the whole answer, did not survive contact with the data.*

Segmenting purely on ToC sections produced **106 chunks with a badly bimodal size distribution**: a healthy 1,775-char median, but a long tail topping out at **21,978 characters spanning 11 pages** ("Chapter on how to perform the rituals of Hajj"). A ~6k-token chunk buries its own answer, cannot be meaningfully reranked, and defeats the precision that motivated structure-aware chunking in the first place.

**Structure gives correct boundaries; it does not give uniform size.** Both are needed. Sections exceeding **2,500 characters** are sub-split on line boundaries, with every part inheriting the full section metadata so citations still resolve to the section. Continuation parts get the section heading prepended, so a chunk lifted out of context still says what it is — cheap contextual retrieval.

Result: **177 chunks**, min 51 / median 1,923 / p90 2,498 / max 2,543 chars. 43 of 107 sections needed splitting.

That is still small enough that brute-force cosine similarity over a numpy array is a viable index (§3.2) — infra complexity remains a choice, not a requirement.

### 1.3 Risk #1 (negation collision) — solve it, don't mitigate it

This is the highest-priority risk in the tracker and it deserves the strongest available answer.

The book pairs opposite rulings in adjacent sections:

- p17 "Those things which nullify wuḍūʾ" → p18 "Those things which do not break wuḍūʾ"
- p51 "Things which nullify ṣalāh" → p55 "Things which do not nullify ṣalāh"

These are near-identical in embedding space and opposite in legal meaning. Dense retrieval will confidently return the wrong one.

The usual mitigations — polarity metadata filtering, query-side polarity classification, hybrid search, reranking — all try to *pick the right member of the pair*. Every one of them is a classifier, and every classifier has an error rate. On a system that issues religious rulings, a silent 5% polarity error rate is not acceptable.

**Better approach: never pick. Always retrieve the whole group.**

Link contrasting sections at index time with a shared `group_id`. When retrieval surfaces any chunk carrying one, unconditionally pull in every other member before the chunks reach the model. The model then sees both "these things nullify wuḍūʾ" and "these things do not" in the same context, and cannot pick the wrong list — it has the whole picture.

```python
def expand_groups(chunks, index):
    """Any retrieved chunk with a group_id drags its whole group along."""
    out = list(chunks)
    seen = {c["id"] for c in chunks}
    for c in chunks:
        if gid := c.get("group_id"):
            for mate in index.by_group(gid):
                if mate["id"] not in seen:
                    out.append(mate)
                    seen.add(mate["id"])
    return out
```

This converts a *retrieval* problem (probabilistic) into a *reading comprehension* problem (which Claude is good at). At this corpus size the extra tokens are irrelevant.

**Correction — these are not all pairs.** The original design assumed a binary `pair_id`. Fasting splits **three ways**: things which do not nullify (p108), things which nullify *and* necessitate kaffārah (p109), and things which nullify *without* kaffārah (p112). The field is therefore an n-ary `group_id`, not a pair.

Curating the table is a one-time manual job. **Six groups exist across the whole book**, found by reading the parsed ToC:

| `group_id` | Members (printed pages) |
|---|---|
| `wudu-nullifiers` | 17 affirmative / 18 negative |
| `ghusl-necessitate` | 18 affirmative / 19 negative |
| `salah-nullifiers` | 51 affirmative / 55 negative |
| `salah-makruh` | 55 affirmative / 59 negative |
| `sawm-nullifiers` | 109 + 112 affirmative / 108 negative — **three-way** |
| `hajj-penalty` | 151 affirmative / 154 negative |

Hand-curate this. Do not detect it automatically; a wrong grouping is worse than none. Ingestion asserts that every declared group resolves to at least one chunk, so a typo fails loudly rather than silently degrading retrieval.

**Cost after sub-splitting (§1.2.1):** a group member may now be several chunks, so expansion pulls more than originally estimated — `salah-nullifiers` is 5 chunks (~12k chars), `sawm-nullifiers` 6. Still cheap against a 1M context, and still the correct behaviour: the point is that the model sees the *complete* contrasting sets.

Two sections are self-contained and need no grouping — p105 ("fasts which require a specified intention from the night, and those that do not") and p115 ("things which are makrūh, not makrūh, and mustaḥab") already present both sides in one section.

**Keep polarity metadata anyway** — not as the primary defence, but as an eval signal. If the model's answer contradicts the polarity of the chunk it cited, that's a detectable failure.

### 1.4 Risk #2 (transliteration variance) — normalize at both ends

Users will type `wudu`, `wuḍūʾ`, `wudhu`, `wuzu`. Two layers:

**Unicode folding.** NFD-decompose, strip combining marks (`unicodedata.category(c) == "Mn"`), recompose. Apply to both the indexed text and the incoming query. This alone handles `wuḍūʾ` → `wudu'`.

```python
import unicodedata

def fold(s: str) -> str:
    d = unicodedata.normalize("NFD", s)
    stripped = "".join(c for c in d if unicodedata.category(c) != "Mn")
    return unicodedata.normalize("NFC", stripped).replace("ʾ", "").replace("ʿ", "").lower()
```

**Alias table.** Folding does not fix `wudhu` → `wudu` or `salaat` → `salah`, because those are different romanization schemes, not diacritic differences. A hand-written alias map covering the ~40 core terms of `ibadat` closes this. Expand the query with its aliases before BM25.

Apply folding to the **BM25 side only**. Leave the dense side on raw text — embedding models handle orthographic variance reasonably well on their own, and folding throws away signal. This is a genuine advantage of hybrid search here, beyond the usual keyword/semantic argument.

### 1.5 Retrieval pipeline

```
query
  ↓ fold + alias-expand
  ↓
  ├── BM25 over text_folded ──┐
  └── dense over text_raw ────┤
                              ↓ reciprocal rank fusion → top 20
                              ↓ cross-encoder rerank → top 5
                              ↓ pair expansion (§1.3)
                              ↓ confidence gate (§1.7)
                              ↓
                        chunks → Claude
```

**Hybrid is not optional here.** Fiqh prose is homogeneous — every chunk is the same register, the same vocabulary, the same author. Dense retrieval's discriminative power is weak when everything looks alike. BM25 rescues the cases where the user names a specific term (`tayammum`, `witr`, `sajdah sahw`) that appears literally in exactly one section.

**Reranking is worth it despite the small corpus.** The usual argument against a reranker is that it adds latency for marginal gain when the first stage is already precise. That argument doesn't hold here, because the homogeneity problem above means first-stage precision is genuinely poor. Retrieve 20, rerank to 5.

### 1.6 Embedding and reranking models

The tracker's open question #5 asks whether multilingual/Arabic capability helps. **It doesn't** — but the original reasoning here was wrong and is worth correcting.

**Correction.** This section originally claimed the corpus contains no Arabic script. It does: Qur'anic and hadith quotations appear throughout. They are set in a font whose encoding does not map, so they extract as Latin-Extended and Cyrillic mojibake (`ُʧ`, `ٰѴَْ Ѵɴ`) — **1.37% of all characters, across 29 pages, peaking at 8.3% of one page**.

The conclusion survives, for a different reason: the Arabic is *unrecoverable*, not *absent*. A multilingual model cannot read mojibake any better than an English one. What the finding actually changes is **ingestion**, which now needs a junk-line filter (§3.3) — left in, that garbage pollutes chunks, BM25 tokens, and embeddings alike.

So: pick a strong **English retrieval** model.

| Role | Recommendation | Why |
|---|---|---|
| Embedding | **Voyage** (`voyage-3.5`, or `voyage-3-large` if quality matters more than cost) | Anthropic's recommended embedding partner; strong on retrieval benchmarks; one vendor for both roles |
| Reranking | **Voyage `rerank-2.5`** | Same vendor, same SDK, no second integration |

Alternatives if you'd rather not add a vendor: Cohere `embed-v4` + `rerank-v3.5`, or OpenAI `text-embedding-3-large` with a local `bge-reranker` cross-encoder. Verify current model names against Voyage's docs before pinning versions — these move.

Note that this adds a second API key alongside `ANTHROPIC_API_KEY`. If you'd rather stay single-vendor, local `sentence-transformers` embeddings are genuinely fine at 400 chunks — but you chose API embeddings, so Voyage is the path.

### 1.7 Risk #3 (hallucinated rulings) — four independent layers

The top failure mode: the model answers from pretrained knowledge of *another madhhab* when Nur al-Idah doesn't cover the question. Prompting alone will not prevent this. Stack four defences so that no single one is load-bearing:

**Layer 1 — API-native citations.** Pass retrieved chunks as `document` content blocks with `citations: {enabled: true}`. Claude then returns citation objects with exact character or page locations pointing into the source text, structurally rather than by being asked nicely. This is meaningfully stronger than prompt-instructed citation, and it's the single highest-value thing in this section.

```python
{
  "type": "document",
  "source": {"type": "text", "media_type": "text/plain", "data": chunk["text_raw"]},
  "title": f"Nur al-Idah — {chunk['bab']} ({chunk['category']}), p{chunk['page_start']}",
  "citations": {"enabled": True},
}
```

Note the constraint: **citations are incompatible with `output_config.format`** (structured outputs) — the combination returns a 400. Pick one per call. Given the choice, citations win for the Q&A path; use structured outputs for the revision path, which doesn't need them.

**Layer 2 — retrieval confidence gate.** If the top reranked score falls below a tuned threshold, do not call the model at all. Return "Nur al-Idah does not appear to cover this." Abstention as control flow, not as model behaviour.

**Layer 3 — system prompt with an explicit authority boundary.** State that Nur al-Idah is the sole authority, that the assistant is Hanafi-only, that it must abstain rather than generalize, and that it must defer to a qualified scholar on anything consequential. Give it explicit permission to say "I don't know" — models abstain far more readily when abstention is framed as a correct answer rather than a failure.

**Layer 4 — programmatic citation verification.** After generation, check that every page number the model cited actually appears in the chunks that were retrieved. A citation to a page that wasn't in context is a hallucination, detectable without a human. Log it, and surface it as a warning in the notebook.

Layers 2 and 4 are code, not prompting. That's deliberate — they're the ones that keep working when the model has a bad day.

---

## 2. Orchestration

### 2.1 Shape: two pipelines, user-selected — not an agent

Decision: **explicit user-selected mode.** No router, no classifier, no sub-agents.

This is the right call and it is worth being explicit about why. Automatic routing is a fixed cost — an extra model call, an extra failure mode, an extra thing to eval — that buys you the ability to skip a two-item menu. At two modes, the menu is cheaper and more predictable than the classifier.

The larger consequence: **neither mode actually needs an agentic loop.** Once you know the mode, the data flow is fixed and linear.

```
Q&A mode                          Revision mode
────────                          ─────────────
user question                     user picks kitab/bab/category
  ↓ retrieve (§1.5)                 ↓ get_section() — deterministic lookup
  ↓ confidence gate                 ↓ gather distractor pool (§2.3)
  ↓ Claude + citations              ↓ Claude + structured output
  ↓ verify citations                ↓ validate against source
answer                            MCQs / flashcards
```

No tool-calling loop, no `while stop_reason == "tool_use"`, no Tool Runner. Two straight-line functions calling `client.messages.create()` once each. That is the whole orchestration layer, and it will be perhaps 150 lines.

Resist the pull to make this agentic because "advanced RAG" sounds like it should be. The sophistication in this project lives in the **retrieval and validation layers** (§1.3, §1.7, §2.3), not in the control flow. An agent loop here would add latency and non-determinism to a problem that is fully specified without it.

**When to revisit:** if you later want multi-hop questions — *"does anything that breaks wuḍūʾ also invalidate tayammum?"* — that genuinely needs a second retrieval informed by the first. At that point, add a tool-calling loop to the Q&A path only, using the SDK's Tool Runner (`client.beta.messages.tool_runner`) so you don't hand-write it. Don't build it before you have a question that needs it.

### 2.2 The two retrieval primitives

Both modes draw on the same index through two functions. Note that only one of them involves embeddings at all.

**`search_fiqh(query, *, kitab=None, category=None) -> list[Chunk]`**
Hybrid search + rerank + pair expansion, per §1.5. Probabilistic. Used by Q&A.

**`get_section(kitab, bab=None, category=None) -> list[Chunk]`**
Deterministic structural lookup against the chunk metadata. No embeddings, no scoring, no failure mode. Used by revision mode for section selection.

`get_section` also fixes a Q&A failure that pure retrieval handles badly: **enumeration questions.** "List all the fard acts of ṣalāh" needs the *complete* list, and top-k retrieval has no notion of completeness — it returns the k best-matching chunks, which may be a partial list. Route enumeration-shaped questions to `get_section` instead, so the answer is grounded in the whole section rather than a similarity-ranked slice of it.

Detecting enumeration shape is a simple heuristic (`list`, `what are the`, `how many`, `all the` + a category keyword), not a model call. Get it wrong and you fall back to search — a cheap failure.

### 2.3 Revision mode: MCQs and flashcards

Scope, per your selection: **MCQs with answer keys** and **flashcards (term → ruling)**. No open-ended questions, no mock papers.

**The distractor problem.** A generated MCQ is only useful if the wrong answers are reliably wrong. If Claude invents distractors freely, it will occasionally invent one that happens to be correct — producing a question with two right answers and, worse, teaching the user something false when they check the key. This is the same class of risk as a hallucinated ruling.

**Solution — draw distractors from the corpus, not from the model.** This mirrors §1.3: exploit the structure rather than trusting the model.

The category metadata makes this nearly free. For a question of the form *"Which of the following is a **fard** of wuḍūʾ?"*:

- The **correct answer** is drawn from `(taharah, wudu, fard)`.
- The **distractors** are drawn from `(taharah, wudu, sunnah)` and `(taharah, wudu, adab)`.

Those distractors are guaranteed wrong, because the book itself classifies them into a different category. Claude's job is reduced to *phrasing* the options, never *choosing* them. The accidentally-correct-distractor failure mode is eliminated structurally rather than checked for after the fact.

This also produces genuinely good exam questions. Fard-vs-sunnah and wajib-vs-sunnah confusions are exactly what a Fiqh exam tests, and the polarity pairs from §1.3 make excellent material too — *"Which of these does NOT break wuḍūʾ?"* with three genuine nullifiers and one non-nullifier.

**Schemas.** Revision mode uses `output_config.format` (structured outputs) rather than API citations — recall from §1.7 that the two are mutually exclusive, and revision output needs machine-parseable shape more than it needs char-level citation spans.

```python
class MCQ(BaseModel):
    question: str
    options: list[str]          # exactly 4
    correct_index: int
    explanation: str
    source_page: int
    kitab: str
    bab: str
    category: str               # the category the CORRECT answer came from

class Flashcard(BaseModel):
    front: str                  # "What are the fard acts of wudu?"
    back: str
    source_page: int
    kitab: str
    bab: str
```

**Post-generation validation** (code, not prompting):

1. `len(options) == 4` and `0 <= correct_index < 4`.
2. `options[correct_index]` corresponds to an item actually present in the correct-answer chunk.
3. Every distractor traces to a chunk in a *different* category than the correct answer.
4. `source_page` falls within the page range of the chunks that were passed in.

Anything failing validation is discarded and regenerated, not shown. At flashcard/MCQ volumes this costs almost nothing and it is the difference between a study aid and a source of confidently-taught errors.

### 2.4 Guardrail boundary

The tracker's open question #8 — when to answer, when to caveat, when to defer. Concretely:

| Situation | Behaviour |
|---|---|
| Covered by Nur al-Idah, high retrieval confidence | Answer with citation |
| Covered, but the book is terse or the question is edge-case | Answer with citation + note the book's brevity |
| Not covered, but within `ibadat` | Abstain: "Nur al-Idah does not address this." Do not generalize. |
| Outside `ibadat` (mu'amalat, family law, etc.) | Abstain and state the scope boundary explicitly |
| Any question with real-world consequence — divorce, inheritance, medical, financial | Answer if covered, but **always** append a defer-to-a-scholar notice |
| Comparative ("what do the Shafi'i say?") | Abstain — single-madhhab corpus, out of scope by design |

The last row matters more than it looks. The model *knows* the Shafi'i position from pretraining, and will happily supply it. That is precisely the hallucination-by-another-madhhab failure the whole design exists to prevent. State it in the system prompt as an explicit prohibition, and put a comparative-question case in the eval set (§4).

---

## 3. Stack and tooling

### 3.1 Dependencies

Everything via `uv`, Python 3.13 in the existing `.venv` (currently empty).

```toml
# pyproject.toml
[project]
name = "ai-fiqh"
requires-python = ">=3.13"
dependencies = [
    "anthropic",          # generation, citations, structured outputs
    "voyageai",           # embeddings + reranking
    "pymupdf",            # PDF text extraction (already verified working)
    "rank-bm25",          # lexical half of hybrid search
    "numpy",              # dense similarity
    "pydantic",           # revision-mode schemas
]

[dependency-groups]
notebook = ["jupyter", "ipykernel"]
ui       = ["streamlit"]           # phase 2
```

`uv add anthropic voyageai pymupdf rank-bm25 numpy pydantic` and `uv add --group notebook jupyter ipykernel`.

### 3.2 Vector store — don't use one

The tracker's open question #4 asks which vector store, weighted toward native hybrid support. The answer at this corpus size is **none**.

400 chunks × 1024 dimensions of float32 is about **1.6 MB**. It fits in L3 cache. A brute-force cosine similarity over the whole corpus is a single `numpy` matrix multiply taking well under a millisecond. There is no index to build, no approximate nearest neighbour to tune, no recall/latency tradeoff to reason about — exhaustive search *is* the fast path.

```python
scores = embeddings @ query_vec      # (n_chunks,) — that's the entire dense retrieval
```

Pair that with `rank_bm25` for the lexical side and hand-write reciprocal rank fusion (about ten lines). Persist the embeddings as a `.npy` file and the chunk metadata as JSON. Total storage layer: two files.

**What you gain by not adding a store:** every stage is inspectable in the notebook. You can print the BM25 ranking next to the dense ranking next to the fused ranking and *see* the polarity collision happening, which is exactly the debugging you need for §1.3. A vector store abstracts that away behind a `.search()` call at precisely the moment you most want to look inside it.

**When to revisit:** if the corpus expands past roughly 50k chunks, or you want persistence across processes with concurrent access. At that point **LanceDB** is the natural upgrade — embedded, no server, native hybrid search and reranking. Write the retrieval layer behind a small interface so the swap is contained.

### 3.3 Ingestion pipeline — **implemented**

`src/ai_fiqh/ingest.py`. One-time script, not on the runtime path. Run with
`uv run python -m ai_fiqh.ingest`.

```
data/Fiqh Class Book.pdf
  ↓ verify printed-page → PDF-index offset on EVERY page      (assertion)
  ↓ parse ToC from PDF indices 0–3 → 107 (title, printed_page)
  ↓ flatten body to a line stream, dropping page numbers
  ↓ strip junk lines (mojibake, §1.6)                          (line-level)
  ↓ locate headings by monotonic forward scan                  (107/107)
  ↓ slice sections; sub-split anything > 2,500 chars (§1.2.1)
  ↓ classify category; assign kitab; attach group_id (§1.3)
  ↓ fold diacritics → text_folded (§1.4)
  ↓ index/chunks.json                                          (177 chunks)
  ↓ [pending Voyage key] embed → index/embeddings.npy
```

**The page-offset risk is closed, better than expected.** The concern was that a wrong printed-page → PDF-index offset would silently corrupt every citation. It turns out **every page's first text line is its own printed page number**, so the offset (1) is asserted per page rather than established once and hoped for. Verified on all 160 content pages; ingestion raises rather than continuing if it ever fails.

**Headings are located by monotonic forward scan, not by page.** Searching forward from the previous match — instead of searching each stated page — handles three separate problems with one mechanism: sections starting mid-page (pp. 14, 16, 18, 19, 20 each host 2–3 sections), repeated heading text, and errors in the book's own ToC.

Three real quirks of this PDF, all found by inspection and handled in code:

| Quirk | Example | Handling |
|---|---|---|
| Footnote marker inside a heading | `The sunan10 of wuḍū’` | `fold(..., drop_digits=True)` when matching |
| The book's own ToC is off by one | `Sujūd al-tilāwah` listed p79, actually p80 | forward scan makes page numbers advisory |
| ToC wording ≠ body wording | ToC `Sujūd al-tilāwah` vs body `Sajdat al-tilāwah` | explicit `TITLE_OVERRIDES` |

The last one is a genuinely different word, so no fuzzy matcher can bridge it. Overrides are written as natural text and folded at import — hand-writing pre-folded keys is error-prone, since `fold` also strips hyphens (this bit once: `"sujud al-tilawah"` never matched, because the folded form is `"sujud altilawah"`).

**Verification performed:** offset asserted on 160 pages; 107/107 headings located; 6/6 polarity groups resolved with a loud warning if any group fails to match a chunk; chunk size distribution inspected; pair contents read by eye.

One inline mojibake character survives (`al-ԑirq̣ al-madani`, p18). That's the correct tradeoff — the filter is line-level, and dropping that line would discard a real ruling to remove one bad glyph.

`poppler` / `pdftoppm` is not installed. Nothing here needs it — the PDF has a real text layer. `brew install poppler` only if a UI later wants rendered pages.

### 3.4 Repository layout

```
ai-fiqh/
├── pyproject.toml
├── data/
│   └── Fiqh Class Book.pdf
├── docs/
│   ├── PROJECT_TRACKER.md
│   └── research.md
├── src/ai_fiqh/
│   ├── ingest.py        # §3.3 — one-time                    ✅ done
│   ├── normalize.py     # §1.4 folding, junk filter, aliases ✅ done
│   ├── index.py         # hybrid search, RRF, group expansion
│   ├── retrieve.py      # search_fiqh / get_section (§2.2)
│   ├── qa.py            # Q&A pipeline (§2.1)
│   ├── revision.py      # MCQ + flashcard generation (§2.3)
│   ├── schemas.py       # pydantic models
│   └── prompts.py       # system prompts, versioned
├── notebooks/
│   └── explore.ipynb    # primary interface, phase 1
├── eval/
│   ├── golden.json      # §4
│   └── run_eval.py
└── index/               # generated, gitignored
    ├── chunks.json      # ✅ 177 chunks
    └── embeddings.npy   # pending Voyage key
```

Keep prompts in `prompts.py` as module-level constants rather than inline strings. You will iterate on the abstention prompt more than any other code in this project, and having it in one place makes the eval loop tractable.

---

## 4. Evaluation

The tracker's open question #9. This is the part most easily skipped and most needed here, because the failure modes are *silent* — a confidently wrong ruling looks exactly like a correct one.

**Golden set: 40–50 questions**, hand-written, JSON, each with expected behaviour rather than expected text. Five categories, all of which must be represented:

| Category | Example | Passes if |
|---|---|---|
| Straightforward covered | "What are the fard acts of wuḍūʾ?" | Correct enumeration, cites the right page |
| **Polarity trap** | "Does laughing aloud break wuḍūʾ?" | Correct side of the pair; §1.3 should make this ~100% |
| **Transliteration variant** | Same question as `wudu` / `wuḍūʾ` / `wudhu` | All three retrieve identically |
| **Out of scope** | "How is inheritance divided?" | Abstains, states scope boundary |
| **Cross-madhhab bait** | "What do the Shafi'i say about this?" | Abstains; does **not** answer from pretraining |

**Metrics**, all computable without a human:

- **Retrieval recall@5** — was the chunk containing the answer in the retrieved set? Requires labelling the correct chunk ID per question; do this while writing the questions.
- **Citation validity** — do all cited pages appear in the retrieved chunks? (§1.7 layer 4, run over the whole set.)
- **Abstention rate on out-of-scope** — should be 100%. Anything less is the primary risk materialising.
- **False abstention rate** — abstaining on covered questions. The failure mode you *create* by tightening the confidence gate; watch it move in the opposite direction when you tune §1.7 layer 2.
- **Polarity accuracy** — on polarity-trap questions specifically.

Those last two are the tuning dial. The confidence gate trades them against each other directly; pick a threshold by running the sweep, not by intuition.

For revision mode, evaluate the **validators** (§2.3) rather than the output: what fraction of generated MCQs pass all four checks on first generation? A low rate means the distractor pool construction is wrong, not that Claude is bad at the task.

Skip `ragas` and `promptfoo`. A JSON file and a `run_eval.py` that prints a table is sufficient, and it is one less framework whose opinions you have to work around.

---

## 5. Open questions now resolved

| # | Question | Resolution |
|---|---|---|
| 1 | Does 164 pages justify RAG? | Yes — for citations and abstention, not for context limits (§1.1) |
| 2 | Chunking strategy | Structure-aware on `(kitab, bab, category)`, from the p2–5 ToC (§1.2) |
| 3 | Is a reranker worth it? | Yes, despite corpus size — prose homogeneity hurts first-stage precision (§1.5) |
| 4 | Vector store | None. numpy + rank_bm25. LanceDB if it ever grows (§3.2) |
| 5 | Multilingual embeddings? | No — corpus is romanized English. Normalize instead (§1.4, §1.6) |
| 6 | Orchestration shape | Two user-selected linear pipelines. No agent loop (§2.1) |
| 7 | How does revision mode change the architecture? | It doesn't touch retrieval — it uses `get_section` + corpus-drawn distractors (§2.3) |
| 8 | Guardrail boundaries | Table in §2.4 |
| 9 | Evaluation | 40–50 question golden set, five categories, five metrics (§4) |

## 6. Build order

1. `git init` — still no version control on any of this.
2. `pyproject.toml` + package skeleton (§3.4).
3. Ingestion (§3.3) — **verify the page offset and eyeball every chunk boundary before moving on.** Everything downstream inherits these errors.
4. Hand-curate the polarity pair table (§1.3).
5. Index + retrieval (§1.5, §3.2), inspected in the notebook.
6. Golden set (§4) — write it *before* the Q&A pipeline, so you have a target.
7. Q&A pipeline with all four abstention layers (§1.7).
8. Run eval, tune the confidence gate.
9. Revision mode (§2.3) + validators.
10. Streamlit UI.

Steps 1–5 are mechanical. Step 6 is where the project's quality is actually decided.

