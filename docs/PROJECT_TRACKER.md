# AI-Fiqh — Project Tracker

**Last updated:** 2026-07-30
**Phase:** Implementation — ingestion done, retrieval next

---

## Status at a glance

| Area | State |
|---|---|
| Corpus | Identified and inspected ✅ |
| Scope decisions | Made ✅ |
| RAG architecture research | Done ✅ — [research.md §1](research.md) |
| Agent orchestration research | Done ✅ — [research.md §2](research.md) |
| Stack & tooling research | Done ✅ — [research.md §3](research.md) |
| Evaluation design | Done ✅ — [research.md §4](research.md) |
| Repo scaffold (`git`, `pyproject`, deps) | Done ✅ |
| `normalize.py` — folding, junk filter, aliases | Done ✅ |
| `ingest.py` → `index/chunks.json` | Done ✅ — **177 chunks, verified** |
| `index.py` — hybrid retrieval | Next — dense half blocked on Voyage key |
| Golden eval set | Not started |
| Q&A / revision pipelines | Not started |

### Ingestion results (2026-07-30)

| Check | Result |
|---|---|
| Printed-page → PDF-index offset | Asserted on **all 160** content pages (offset = 1) |
| ToC headings located | **107 / 107** |
| Polarity groups resolved | **6 / 6**, including the three-way fasting split |
| Chunks produced | **177** — min 51, median 1,923, p90 2,498, max 2,543 chars |
| Sections needing sub-split | 43 / 107 |
| Mojibake | 1.37% of chars, 29 pages — stripped at line level |

> **Note on how this got done:** sub-agent dispatch failed twice (see *What didn't
> work*). The research was completed inline instead, interactively, on 2026-07-27.
> `docs/research.md` now exists and resolves all nine open questions.

---

## Environment

- **Path:** `/Users/rifatmahammod/Developer/personal-projects/ai-fiqh`
- **Python:** 3.13.0 via `uv` venv at `.venv/` — **zero packages installed**
- **Not a git repo** — no version control initialised yet
- No `pyproject.toml`, no source tree
- **No poppler / `pdftoppm`** on this machine. PDF text extraction works via
  `uv run --with pymupdf python …`; anything needing page rasterisation will
  require `brew install poppler` first.

---

## Corpus — established by inspection

**`data/Fiqh Class Book.pdf`** = *Nur al-Idah* by Abu al-Ikhlas Hasan al-Shurunbulali,
short summarised English translation.

| Property | Value |
|---|---|
| Pages | 164 |
| Embedded PDF outline | **None** (`get_toc()` → 0 entries) |
| Human-readable ToC | Pages 2–5, with printed page numbers |
| Text layer | Present — text-based PDF, not scanned |
| Script | English prose, heavy Arabic transliteration with combining diacritics |

**Structural asset:** the book is highly regular. Each act of worship is decomposed
into the same legal categories — *fard actions / wajibat / sunan / adab (etiquettes) /
makruhat (disliked) / nullifiers / things which do NOT nullify*. These are natural,
legally meaningful chunk boundaries and should drive the chunking strategy.

---

## Decisions made

| Decision | Value | Rationale |
|---|---|---|
| Madhhab | **Hanafi** | Follows the source book |
| Domain | **`ibadat` only** — Taharah, Salah, Zakat, Sawm, Hajj | Book's scope; no mu'amalat, no family law |
| Corpus expansion | **Out of scope for now** | Single-source grounding is the cleanest citation story |
| Authority model | Nur al-Idah is the sole authority | Agent must abstain outside it rather than use pretrained knowledge |
| Dependency mgmt | `uv` | Pre-existing project convention |

---

## Decisions added 2026-07-27

| Decision | Value | Rationale |
|---|---|---|
| RAG vs. long-context | **RAG** | Corpus fits in context, but RAG is what buys citations + abstention |
| Generation | Claude API | Best abstention and citation discipline |
| Embeddings + rerank | Voyage (hosted API) | English retrieval quality; one vendor for both roles |
| Vector store | **None** — numpy + `rank_bm25` | 400 chunks ≈ 1.6 MB; exhaustive search *is* the fast path |
| Orchestration | Two user-selected linear pipelines | No router, no agent loop — nothing here needs one |
| Revision output | MCQs with answer keys + flashcards | Open-ended questions and mock papers cut from scope |
| Interface | Jupyter notebook → Streamlit later | |

> ⚠️ **`.claude/CLAUDE.md` is now stale.** It says *"Automatically span between
> sub-agents depending on what the user asking."* The chosen design has no routing
> and no sub-agents — mode is user-selected. Update or drop that line.

---

## Identified risks

### 1. Negation / polarity collision — **highest priority**

The book pairs opposite rulings in adjacent sections:

- "Those things which nullify wuḍū'" (p17) → "Those things which do not break wuḍū'" (p18)
- "Chapter regarding those things which nullify ṣalāh" (p51) → "Things which do not nullify ṣalāh" (p55)

These are near-identical in embedding space and **opposite in legal meaning**. Naive
chunking plus dense-only retrieval will confidently return the wrong one and produce a
wrong ruling.

**Solved in design; half implemented — [research.md §1.3](research.md).** Don't try to
pick the right member; every approach that does is a classifier with an error rate.
Instead link contrasting sections with a shared `group_id` at index time and **always
retrieve the whole group**. The model sees nullifiers and non-nullifiers together and
cannot choose wrong. Converts a probabilistic retrieval problem into a
reading-comprehension one.

- ✅ `group_id` attached during ingestion; **6 groups**, all resolving, asserted at build time.
- ⏳ `expand_groups()` at query time — lands with `index.py`.

> **Corrected:** these are not all *pairs*. Fasting splits three ways (nullify +
> kaffārah p109, nullify without kaffārah p112, do not nullify p108), so the field is
> an n-ary `group_id`. Sub-splitting also means one member can be several chunks —
> `salah-nullifiers` expands to 5 chunks (~12k chars). Cheap, and still correct.

### 2. Transliteration variance

Users will type `wudu` / `wuḍū'` / `wudhu` inconsistently.

**Solved and implemented — [research.md §1.4](research.md), `src/ai_fiqh/normalize.py`.**
NFD-decompose → strip combining marks → recompose, plus a hand-written alias table
(~80 `ibadat` terms; folding fixes `wuḍūʾ`, aliases fix `wudhu`). Applies to the **BM25
side only** — the dense side stays on raw text, since embeddings handle orthographic
variance and folding discards signal.

> Watch out: `fold()` also strips hyphens and apostrophes. Any literal folded string
> written by hand must account for that — `"sujud al-tilawah"` folds to
> `"sujud altilawah"`. This bit once during ingestion. Fold at import instead.

### 3. Hallucinated rulings

Top failure mode: the model answers from pretrained knowledge of *another madhhab* when
Nur al-Idah doesn't cover the question.

**Solved — [research.md §1.7](research.md).** Four independent layers, so nothing is
load-bearing alone: (1) API-native citations via `citations: {enabled: true}` on
document blocks; (2) a retrieval-confidence gate that abstains in *code* before the
model is called; (3) system prompt with an explicit authority boundary; (4) programmatic
verification that every cited page was actually in context. Layers 2 and 4 are code —
they keep working when the model has a bad day.

---

## Open questions — all resolved 2026-07-27

All nine are answered in [research.md §5](research.md). Summary:

| # | Question | Resolution |
|---|---|---|
| 1 | Does 164 pages justify RAG? | Yes — for citations and abstention, not for context limits |
| 2 | Chunking strategy | Structure-aware on `(kitab, bab, category)`, anchored to the p2–5 ToC |
| 3 | Reranker worth it? | Yes — Fiqh prose is homogeneous, so first-stage precision is poor |
| 4 | Vector store | None. numpy + `rank_bm25`. LanceDB only if the corpus grows |
| 5 | Multilingual embeddings? | No — corpus is romanized English. Normalize instead |
| 6 | Orchestration shape | Two user-selected linear pipelines; no agent loop |
| 7 | How does revision mode change things? | It doesn't touch retrieval — `get_section` + corpus-drawn distractors |
| 8 | Guardrail boundaries | Six-case table in research.md §2.4 |
| 9 | Evaluation | 40–50 question golden set, five categories, five metrics |

### New open question

- **No `ANTHROPIC_API_KEY` and no `ant` CLI on this machine.** Nothing runs until
  there's a credential. Also needs a Voyage key for embeddings + reranking.

---

## What didn't work

**Sub-agent dispatch failed twice, zero output both times.**

| Attempt | Outcome |
|---|---|
| 1 — `research-agent` (background) | Killed by session limit (reset 14:20 BST). It had spawned its own sub-agent ("Research vector stores and embeddings"); both died together. |
| 2 — `research-agent`, fan-out forbidden | API error: response stalled mid-stream. 0 tool uses, ~8k tokens consumed, no files written. |

**Lessons:**
- Sub-agents that fan out multiply consumption and fail as a group. Forbid fan-out on
  budget-constrained tasks.
- Long research briefs that "think then write" lose everything on failure. Instruct
  agents to write a skeleton file **first** and fill sections incrementally.
- Two consecutive failures = stop dispatching and do the work inline.

---

## Config fixes applied

**Invalid tool names in agent frontmatter.** `research-agent.md` and `coding-agent.md`
listed `Execute`, `Search`, `Web`, `Todo` — none of which are real Claude Code tools.
The agents would have launched silently stripped of web search, grep, and bash, with no
error surfaced. Replaced with real names: `Bash`, `Glob`, `Grep`, `WebSearch`,
`WebFetch`, `Write`, `TodoWrite`.

**Outstanding —** `.claude/agents/project-tracker.md` line 3 has `description::`
(double colon). This is malformed YAML frontmatter and may prevent the agent from
loading. Not yet fixed.

---

## Next steps

Full build order in [research.md §6](research.md). Steps 1–5 are done. Remaining:

1. **Obtain the Voyage API key** — blocks the dense half of retrieval only.
2. `index.py` — BM25 (no key needed) + dense + RRF + `expand_groups()` + rerank.
3. **Write the golden eval set** (research.md §4) *before* the Q&A pipeline, so
   there's a target to build against.
4. Q&A pipeline with all four abstention layers (research.md §1.7).
5. Run eval; tune the confidence gate against false-abstention rate.
6. Revision mode: MCQs + flashcards, with corpus-drawn distractors (research.md §2.3).
7. Streamlit UI.

> Step 3 is where the project's quality is actually decided — everything after it is
> tuning against a target, and everything before it was plumbing.

### Known limitation to revisit

Category classification leaves **132 of 177 chunks as `general`** — correct, since most
sections are topical ("Tayammum", "Chapter of Witr") rather than category-shaped. But
the corpus-drawn distractor scheme (research.md §2.3) only works where real categories
exist (fard / wajib / sunnah / adab / makruh ≈ 25 chunks). That is enough for MCQs on
the core enumerations, which is the point — but revision coverage will be uneven across
the book, and Hajj especially is thin.
