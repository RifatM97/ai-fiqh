# AI-Fiqh — Project Tracker

**Last updated:** 2026-08-03
**Phase:** Implementation — retrieval working end-to-end, Q&A pipeline next

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
| `index.py` — hybrid retrieval | Done ✅ — **smoke-tested, polarity case passes** |
| `index/embeddings.npy` | Built ✅ — 177 × 1024, `voyage-4-large` |
| Golden eval set | Written ✅ — **40 questions, all five §4 categories** |
| Eval harness (`eval/run_eval.py`) | Not started — needs `chunk_id` labels first |
| `retrieve.py` / `qa.py` / `revision.py` | Not started |

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

### Retrieval results (2026-08-03)

`src/ai_fiqh/index.py` implements the full §1.5 pipeline: fold + alias-expand →
BM25 ∥ dense → RRF → rerank → group expansion, with a `SearchTrace` that keeps
every intermediate ranking for notebook inspection.

| Check | Result |
|---|---|
| Embeddings built | **177 × 1024** float32, `voyage-4-large`, 89,210 tokens |
| Cache keying | SHA-256 over model + dim + every chunk id and body — stale corpus refetches, unchanged corpus does not |
| Smoke query | `"does laughing aloud break wudu"` |
| Polarity behaviour | **Works.** Rerank put the *nullifiers* chunk at #1 and pushed *non-nullifiers* to #5; group expansion lifted it to #2 |
| Confidence gate | Top rerank score 0.832 → PASS |

> **The polarity design is now proven on a live query, not just at index time.**
> This was risk #1 and the single most load-bearing bet in the architecture.
> Dense retrieval alone ranked the two contrasting sections #1 and #2 — i.e. it
> *did* confuse them, exactly as predicted — and expansion made that harmless.

---

## Environment

- **Path:** `/Users/rifatmahammod/Developer/personal-projects/ai-fiqh`
- **Python:** 3.13.0 via `uv` venv at `.venv/`, all deps installed
- **Git:** initialised; one commit (`04966b3`) on `main`
- **Credentials:** `.env` at repo root holds `ANTHROPIC_API_KEY` and
  `VOYAGE_API_KEY`. Gitignored (`.gitignore:7`), loaded via `python-dotenv`.
- **Voyage billing:** payment method added 2026-08-03 — see *Throttling* below.
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

## Decisions added 2026-08-03

| Decision | Value | Rationale |
|---|---|---|
| Embedding model | **`voyage-4-large`**, 1024 dims | research.md §1.6 named `voyage-3.5` / `voyage-3-large`; **both are deprecated** as of 2026-08. At 177 chunks the cost gap to `voyage-4` is a rounding error, and §1.5's homogeneity problem makes retrieval quality the binding constraint |
| Reranker | **`rerank-2.5`** | Still current; §1.6's recommendation survives unchanged |
| Env loading | `python-dotenv`, added to `[project.dependencies]` | `.env` at repo root, already gitignored |
| Embedding cache invalidation | SHA-256 fingerprint over model + dim + chunk contents | Re-ingesting must refetch; re-running must not |

> **Lesson worth keeping:** research.md §1.6 said *"verify current model names
> against Voyage's docs before pinning versions — these move."* That warning paid
> off within a week. Re-check before any future pin.

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
- ✅ `expand_groups()` at query time — implemented in `index.py`, **verified on a
  live query 2026-08-03**. Siblings are appended directly after the hit that
  dragged them in, so trace ordering stays readable.

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

### New open question — resolved 2026-08-03

- ~~**No `ANTHROPIC_API_KEY` and no Voyage key on this machine.**~~ Both now live
  in `.env`. Voyage is exercised and working; the Anthropic key is present but
  **not yet exercised** — nothing calls Claude until `qa.py` exists.

### Open questions added 2026-08-03

1. **`MIN_RERANK_SCORE` is an unvalidated placeholder (0.40).** It is the §1.7
   layer-2 abstention gate and is currently a guess. It must be set by sweeping
   it against false-abstention rate on the golden set (step 8), not by intuition.
   The one live query scored 0.832, so 0.40 is not obviously wrong — but one data
   point is not a threshold.
2. **Golden set has no `chunk_id` labels**, so retrieval recall@5 cannot be
   computed — and that is the metric that says whether `index.py` actually works.
   Labelling is the gating task for the eval harness.

---

## Golden eval set (2026-08-03)

Written by hand. Started as `eval/golden-eval-set.xlsx` — 30 content questions
(Purity 10 / Salah 11 / Zakah 4 / Fasting 3 / Hajj 2) with free-text answers,
all of them the "straightforward covered" category. Then extended to the full
§4 design as **`eval/golden-eval-set.json`, 40 questions**, all five categories
represented:

| Category | Count |
|---|---|
| Straightforward covered | 10 |
| Transliteration variant | 9 |
| Polarity trap | 8 |
| Out of scope | 7 |
| Cross-madhhab bait | 6 |

Schema is `{id, category, question, expected_behavior}` — behaviour rather than
reference text, which is right: "abstains correctly" has no gold string to match.

**Two gaps before `run_eval.py` can be written:**

- **No `chunk_id` field.** Recall@5 needs the correct chunk labelled per question.
  §4 says to do this while writing the questions; it wasn't, so it is now a
  separate pass. Only applies to the 27 in-scope questions — out-of-scope and
  cross-madhhab bait have no correct chunk by construction.
- **The 30 original content questions and their answers are not in the JSON** —
  only 10 straightforward-covered survived the extension. The xlsx still holds
  them. Worth deciding whether to merge them back; they are real hand-written
  Fiqh content and re-deriving them is not free.

---

## Known issues in `index.py` — found by the smoke test, not yet fixed

1. **`expand_groups()` mutates the `Scored` objects it was handed.** The final
   renumbering loop writes `s.rank` in place, and `trace.reranked` holds the same
   objects, so inspecting the RERANK stage after expansion shows corrupted ranks
   (observed: `1, 3, 4, 4, 5`). Fix with `dataclasses.replace` so expansion
   returns copies. **This only damages the trace, not the retrieved set** — but
   the trace is the whole reason there is no vector store, so it matters.
2. **A chunk that was already reranked gets relabelled `group-expansion`** if a
   sibling pulls it in first, and inherits the trigger's score instead of its own.
   Cosmetic, same root cause, fix in the same pass.

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

### Voyage free-tier throttling (2026-08-03) — resolved

The first embedding build died immediately: a Voyage account with no payment
method is capped at **3 RPM / 10K TPM**, and the initial 64-chunk batches were
~32K tokens each. The corpus is 89,210 tokens, so a compliant build would have
taken ~9 minutes.

Fixed in `index.py` rather than worked around, since the pacing logic is cheap
and the constants are one edit away from unthrottled:

- Token-aware batching via Voyage's **local** tokenizer (`count_tokens`), so
  batch sizes are measured, not guessed from character counts.
- Sleep between requests derived from the TPM budget, plus a floor from the RPM
  ceiling; exponential backoff on `RateLimitError`.
- **Checkpointing after every batch** to `index/embeddings.partial.npz`, keyed by
  the same fingerprint. A nine-minute build that loses everything to one 429 at
  chunk 150 is the failure mode worth engineering against. The checkpoint is
  deleted on success — verified gone after the real build.

**A payment method was added the same day**, so `EMBED_BATCH_TOKENS`,
`TPM_BUDGET` and `MIN_REQUEST_INTERVAL` can now be raised and the build drops to
seconds. They are the only thing making it slow. *Not yet raised.*

---

## Config fixes applied

**Invalid tool names in agent frontmatter.** `research-agent.md` and `coding-agent.md`
listed `Execute`, `Search`, `Web`, `Todo` — none of which are real Claude Code tools.
The agents would have launched silently stripped of web search, grep, and bash, with no
error surfaced. Replaced with real names: `Bash`, `Glob`, `Grep`, `WebSearch`,
`WebFetch`, `Write`, `TodoWrite`.

**Resolved 2026-08-03 —** `.claude/agents/project-tracker.md` line 3 previously had
`description::` (double colon), malformed YAML that may have prevented the agent
loading. Line 3 now reads a single colon; the frontmatter parses.

---

## Next steps

Full build order in [research.md §6](research.md). **Steps 1–6 are done** — both
credentials are in place, retrieval works, and the golden set exists. Remaining:

1. **Fix the two `expand_groups()` trace bugs** (see *Known issues* above). Small,
   and worth doing before the trace gets relied on for tuning.
2. **Label `chunk_id` on the 27 in-scope golden questions.** Gates recall@5, which
   gates knowing whether retrieval works. Propose labels by running each question
   through `Retriever.search` and spot-check the answers by hand.
3. `eval/run_eval.py` — a table, no framework (research.md §4).
4. Q&A pipeline (`qa.py`) with all four abstention layers (research.md §1.7).
   First thing to actually exercise `ANTHROPIC_API_KEY`.
5. Run eval; **sweep `MIN_RERANK_SCORE`** against false-abstention rate.
6. Revision mode: MCQs + flashcards, with corpus-drawn distractors (research.md §2.3).
7. `notebooks/explore.ipynb` — the phase-1 interface. `SearchTrace.show()` exists
   to be driven from here; nothing uses it yet.
8. Streamlit UI.

> The golden set existing is *not* the same as it being usable. Step 2 is the real
> gate now — until questions carry chunk labels, the eval can measure abstention
> and citation validity but not retrieval, which is the half just built.

Also outstanding, unblocked, low cost: raise the Voyage throttle constants now
that billing is enabled, and update the stale routing line in `.claude/CLAUDE.md`.

### Known limitation to revisit

Category classification leaves **132 of 177 chunks as `general`** — correct, since most
sections are topical ("Tayammum", "Chapter of Witr") rather than category-shaped. But
the corpus-drawn distractor scheme (research.md §2.3) only works where real categories
exist (fard / wajib / sunnah / adab / makruh ≈ 25 chunks). That is enough for MCQs on
the core enumerations, which is the point — but revision coverage will be uneven across
the book, and Hajj especially is thin.
