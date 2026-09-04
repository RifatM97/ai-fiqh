# AI-Fiqh — Project Tracker

**Last updated:** 2026-08-05
**Phase:** Build order complete — all ten steps of research.md §6 are done,
including the Streamlit UI. Remaining work is quality/hardening, not new
components; see *Still open* and *Next steps* below.

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
| Golden eval set | Written ✅ — **40 questions, 8/category, all with reference answers** |
| Golden set labelling | Done ✅ — `expected_chunk_ids`, `should_abstain`, `variant_group` |
| `MIN_RERANK_SCORE` | Measured ✅ — **0.74**, gate 40/40 |
| Eval harness (`eval/run_eval.py`) | Done ✅ — scored harness + `--sweep` gate-trade-off mode, zero model calls |
| `prompts.py` / `qa.py` | Done ✅ — §2.1 linear pipeline, all four §1.7 abstention layers |
| Eval run `20260804-103342` | Done ✅ — **40/40 behaviour, 24/24 ruling agreement, 0/24 false abstentions** — see *Eval results* below for caveats |
| `notebooks/explore.ipynb` | Done ✅ — 26 cells / 8 sections, committed unexecuted (§1–3 verified run, §4–7 verified on an earlier execution) |
| `retrieve.py` | **Dropped ✅** — never needed; §2.2's primitives are `Retriever.search` / `Retriever.get_section` |
| `revision.py` / `schemas.py` | Done ✅ — MCQs with corpus-drawn distractors, flashcards, `build_deck` for Zakah/Hajj |
| `app.py` — Streamlit UI (§2.1) | Done ✅ — **build order complete**, verified end-to-end via `AppTest`, uncommitted |

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

### `expand_groups()` bugs — fixed 2026-08-03

Both bugs logged below under *Known issues* are now fixed:

- **Mutation bug:** the final renumbering loop wrote `s.rank` in place on the
  caller's reranked list, corrupting `trace.reranked` after the fact (RERANK
  stage showed `1, 3, 4, 4, 5`). Fixed by copying via `dataclasses.replace`
  instead of mutating in place.
- **Misattribution bug:** a chunk the reranker had already found independently
  got relabelled `"group-expansion"` and inherited the *trigger's* score.
  Fixed — such chunks now keep their own score and provenance; only genuinely
  new siblings are marked as expanded.

Verified on the same smoke query: RERANK now reads `1, 2, 3, 4, 5`, and the
expanded wuḍūʾ sibling shows its own real score `0.5195` instead of the
inherited `0.8320`.

---

## 2026-08-04 — Q&A pipeline, eval harness, notebook

**Correction to the previous pass:** it recorded "nothing committed" for the
labelling/gate-calibration work. That is stale. Two commits landed:

- **`f4d6663`** "Label golden eval set and calibrate the abstention gate" —
  the labelling work already documented above, plus the `MIN_RERANK_SCORE`
  0.40 → 0.74 change, `.gitignore` credential patterns, and the SSH keypair
  moved out of the repo root to `~/.ssh/`.
- **`f751045`** "Add Q&A pipeline, eval harness, and exploration notebook" —
  everything below.

### New files

- **`src/ai_fiqh/prompts.py`** — versioned system prompts,
  `QA_PROMPT_VERSION = "qa-v1"`. Encodes §2.4's guardrail table. **Deliberate
  omission worth recording:** it contains no self-verification instruction,
  because Opus 5 already self-verifies and instructing it again produces
  over-verification.
- **`src/ai_fiqh/qa.py`** — the §2.1 linear pipeline, all four §1.7 layers:
  (1) API-native citations on document blocks; (2) a confidence gate
  abstaining in code before any model call; (3) the authority prompt; (4)
  `verify_citations()`, flagging any page named in prose that no supplied
  chunk covers. Plus §2.2 enumeration routing — a regex heuristic expands the
  top hit to its whole section via `get_section`, merged rather than
  substituted so §1.3 group expansion survives.
- **`eval/run_eval.py`** — the scored harness, plus a `--sweep` mode that
  plots the gate trade-off with zero model calls.
- **`eval/results/20260804-103342.json`** — the run, stamped with prompt
  version / model / effort / gate so runs stay comparable.
- **`notebooks/explore.ipynb`** — 26 cells, 8 sections. Closes the
  "notebooks/ empty" gap open since research.md §3.4.
- **`.gitattributes`** — `nbstripout` filter rule.

### Model and API decisions (research.md §1.6/§1.7 predate these)

- Model is **`claude-opus-5`**, not what research.md assumed. `MAX_TOKENS =
  16000`, `EFFORT = "high"`.
- Thinking is **on by default** on Opus 5, and `max_tokens` caps thinking
  *plus* answer text — hence the generous ceiling for short answers.
- Refusal fallback (`fallbacks: "default"`, beta
  `server-side-fallback-2026-07-01`) enabled by default, degrading
  gracefully to the non-beta path if the beta isn't enabled on the key.
- System prompt is prompt-cached (`cache_control` on the system block) —
  byte-stable across questions, so it writes once and every later call reads
  it.
- **§1.7's citations/structured-outputs conflict is confirmed real**:
  citations 400 alongside `output_config.format`. Q&A takes citations;
  revision mode (§2.3) must take structured outputs instead. The eval judges
  use structured outputs precisely because a grader needs no citations.

### Eval results

Behaviour 40/40 · ruling agreement 24/24 · abstention 16/16 · polarity 8/8 ·
recall@context 24/24 · citation validity 40/40 · variant agreement 3/3 ·
false abstentions 0/24. Latency median 10.8s, max 31.7s; 80s wall for all 40
at 5 workers.

**The caveats matter as much as the numbers:**

1. **Ruling agreement (24/24) is the only genuinely new signal** — everything
   prior measured behaviour, not correctness. It is judged against the
   hand-written reference answers.
2. **Judges were negative-controlled: 7/7 on planted failures.** This matters
   because "the judge passed everything" is the most likely explanation for a
   perfect score. The two discriminating cases both passed: a missing
   enumeration item scores `incomplete` not `agrees`; a decline that still
   cites the Hanafi position scores `declined` not `leaked`.
3. **The 16/16 abstention figure is circular** — the gate was fitted on this
   set, and all 16 fired at layer 2 without a model call. The independent
   evidence is a **gate-disabled run (`gate=0.0`) where layer 3 alone held
   16/16**, declining every comparative/out-of-scope question while still
   giving the Hanafi ruling on the underlying topic. That gate-disabled
   result, not the 16/16 headline number, is the meaningful one.
4. **The golden set is now saturated.** 40/40 with zero failures means it
   cannot detect a regression when `qa-v1` is edited, nor distinguish a
   better config from a worse one. It has stopped being informative. Open
   question below: it needs harder cases — questions the book answers
   ambiguously or tersely, near-miss polarity pairs — with Hajj the obvious
   gap given the known thin coverage.

### Finding the notebook surfaced — `sawm-kaffarah` variant group

The group **disagrees at top-1 retrieval**, though the harness scored
variant agreement 3/3 (it compares verdicts, and all three answered
correctly). Q20 retrieves
`109-chapter-on-those-things-which-nullify-the-fast-...` while Q21/Q22 both
retrieve `111-chapter-on-kaffarah-...`.

The cause is not transliteration: Q21 vs Q22 is the real spelling pair
(`kaffarah`/`kaffāra`) and those agree perfectly. Q20 **omits the Arabic term
entirely** ("What is the penalty for intentionally breaking a fast?"), so
BM25 has no lexical anchor and the nullifiers chapter wins. The group
conflates a term-presence variant with spelling variants. Nothing is broken —
all three retrieve the right chunk within top-5 and answer correctly — but
the 3/3 overstates what was verified. Open item: either split Q20 into its
own group, or define the metric on recall rather than top-1.

### Tooling

- `matplotlib` and `nbstripout` added to the `notebook` dependency group.
- `nbstripout`: the filter **rule** ships in `.gitattributes`, but the filter
  **binary** is configured per clone in `.git/config`. A fresh clone must run
  `uv run nbstripout --install --attributes .gitattributes` or outputs
  silently leak into diffs.
- Chart palette validated with the dataviz checker (CVD ΔE 24.7 against a
  ≥8 target) before use.

### Revision mode — started, uncommitted

> **Stale as of 2026-08-05:** this subsection describes the state on
> 2026-08-04. Both files below are now committed (`b4c59f2`, `c949e87`) and
> exercised — see *2026-08-05* above and *Status at a glance*. Left as-is for
> the historical design record.

`src/ai_fiqh/schemas.py` (103 lines) and `src/ai_fiqh/revision.py` (426
lines) exist on disk, untracked. Design so far: Claude never picks the
correct answer or invents distractors — it only phrases items this code
already selected. Distractors are drawn from the book's own category
structure (`(kitab, category)` vs. the *other* categories in the same
`kitab`) or from polarity groups (§1.3), so a distractor is guaranteed wrong
because the book itself files it elsewhere, not because a model judged it
so. `schemas.py` splits **output** shapes (`MCQ`, `Flashcard`) from
**generation** shapes (`PhrasedOptions`, `GeneratedFlashcard`) for the same
reason — the model returns phrasings, not decisions. Uses
`output_config.format`, so per the citations/structured-outputs conflict
above it cannot carry API citations; provenance instead comes from the
`chunk_id` each item was drawn from. Not yet reviewed or run — record
progress here rather than treat it as done.

---

## 2026-08-05 — Streamlit UI: the build order is complete

**Headline: all ten steps of research.md §6 are now done.** The Streamlit UI
(§2.1) was the last of them. Everything after this point is quality work
against what already exists, not new components — see *Still open* below.

### Commits since the last pass

Three, all now on `main`:

| Commit | Summary |
|---|---|
| `b4c59f2` | Add revision mode: MCQs with corpus-drawn distractors, and flashcards |
| `c949e87` | Add flashcard decks so Zakah and Hajj have revision coverage |
| `5849ff9` | Drop retrieve.py from the design; record why rather than deleting it |

**Push status, checked directly this pass:** `git fetch origin` succeeded and
`git rev-list --left-right --count origin/main...main` reports `0 0` —
`origin/main` is at `5849ff9`, matching local `main` exactly. All three
commits above are confirmed on the remote. Note for the record: an earlier
check in this pass had `git fetch` fail with "Could not resolve host:
github.com" (no network at that moment), so the push state was treated as
unverified until the retry above succeeded — worth re-checking again next
time rather than assuming either state holds.

**Still uncommitted on disk:** `src/ai_fiqh/app.py` (new) and
`src/ai_fiqh/normalize.py` (modified — adds `display_title()`), plus the
`pyproject.toml` / `uv.lock` changes that add the `ui` dependency group.

### New: `src/ai_fiqh/app.py` — the Streamlit UI (uncommitted)

Three tabs: **Ask**, **Practice questions**, **Flashcards**. Per §2.1 the tab
*is* the mode — no router, no classifier, no agent loop. The file
deliberately contains no retrieval logic, no prompts and no validation; it
calls `qa.answer`, `revision.generate_mcqs`, `revision.generate_flashcards`,
and its real job is surfacing what a user must not miss.

Design points worth recording:

- `get_retriever()` / `get_client()` are `@st.cache_resource`, so embeddings
  load once per server rather than per interaction.
- **Abstention renders as `st.info`, not an error**, with a "Why it
  abstained" expander showing the gate score and whether a model call was
  made. Abstaining is a correct outcome and must not look like a failed
  request.
- **Layer 4 renders as `st.error`** — if a page is named that was not in
  context, the answer is marked unreliable.
- `mcq_targets()` probes which `(kitab, category)` pairs can actually build a
  distractor pool, so **the MCQ Book dropdown offers 3 books while
  Flashcards offers 5**. Zakah and Hajj never appear as buttons that cannot
  work.
- A "Retrieval trace" expander shows the final ranking with `← group
  expansion` marking chunks that only reached the model because §1.3 pulled
  in their polarity sibling.
- `ui = ["streamlit"]` added to the `pyproject.toml` dependency groups
  (research.md §3.1 had planned this) — confirmed present in the diff.

### Verified end-to-end via Streamlit's `AppTest` harness

Not just "it parses" — the script was executed and driven:

| Flow | Result |
|---|---|
| Q&A, covered question | 4 citations, pages 13–19, enumeration routing fired |
| Q&A, out-of-scope | Abstained at layer 2 — gate 0.74 vs best match 0.379, no model call |
| Practice questions | 2 stems, 4 options each, answerable |
| Flashcards | 4–5 cards with page provenance (2 clean runs) |
| 529 overload | Warning shown, no traceback |

### Bug found by testing: unhandled API errors

A real `529 Overloaded` from the API surfaced as a **raw Python traceback in
the UI**. Fixed with a `guarded()` wrapper handling rate limits, 529
overloads, out-of-credit 400s, and connection errors, returning `None` so
previous output is left alone; `max_retries` raised from the default 2 to 5
(`anthropic.Anthropic(max_retries=5)`). Verified both by injecting a
synthetic 529 and by two genuine overloads that occurred during testing.

### New: `display_title()` in `normalize.py` (uncommitted)

Strips only Private Use Area and control codepoints, for rendering section
titles in dropdowns. The Hajj chapter on visiting the Prophet ends in ``,
the PDF's unmapped ﷺ. Applied only at the display boundary — the raw title
stays the key, because `get_section` matches on it exactly.

### Important: measured defect in `is_junk_char` — documented, deliberately NOT fixed

This is a latent correctness issue in a core module; the reasoning is
recorded here so it isn't lost, and it's now also written into the
function's docstring in code.

**The defect.** `is_junk_char` treats any codepoint ≥ `0x250` outside a small
allow-list as junk. That cutoff predates needing Latin Extended Additional
(`U+1E00–U+1EFF`), which is exactly where this transliteration's dot-below
letters live — `ṣ ḍ ḥ ṭ ẓ` are all reported as junk, while the macron
letters `ā ī ū` sit below the cutoff and pass. The function is therefore
inconsistent about the very scheme the corpus is written in.

**Why it has not broken anything.** Every caller applies a *ratio* threshold
rather than filtering per character, so a few diacritics in normal-length
text dilute below it. That is luck, not design.

**Measured cost across the whole corpus** (measured, not reasoned — an
earlier claim that ingestion lost nothing was wrong and was corrected):

- `strip_junk_lines` at 0.30 drops **72 lines, 71 of them genuine mojibake**.
  The exception is p109's `'q̣aḍā’'` at ratio 0.33 — the wrapped second half
  of the heading "Chapter on those things which nullify the fast and
  necessitate kaffārah with / qaḍāʾ". Harmless: the heading still matched on
  its first line, the full title survives in the chunk's `bab`, and the
  phrase recurs in the body two lines later. Had the diacritics counted as
  legitimate it would have scored 0.17 and survived.
- Revision's tighter 0.08 ceiling rejects exactly **2 items, both genuinely
  unmappable Arabic**.

**Where it did bite:** the first version of `display_title` reused
`is_junk_char` and turned "The Book of Ṣalāh" into "The Book of alāh" across
44 titles. That is what prompted the investigation.

**Why not fixed:** correcting the range changes chunk text → forces a
re-ingest → invalidates `index/embeddings.npy` → potentially invalidates the
golden set's `expected_chunk_ids` and with them the 40/40 eval. A wide blast
radius to recover one heading fragment that cost nothing. The defect and its
measured cost are now written into the function's docstring along with this
reasoning. **Log as an open item: if the corpus is ever re-ingested for
another reason, fixing the `0x250` boundary should ride along with that
change.**

### Still open (carry forward, do not mark done)

1. **Golden set is saturated** at 40/40 — cannot detect a regression in
   `qa-v1`. Needs harder/ambiguous/terse cases, Hajj prioritised.
2. **Merge the remaining 20 of the 30 xlsx content questions** into the JSON
   golden set.
3. **`sawm-kaffarah` variant group** conflates a term-presence variant (Q20)
   with spelling variants (Q21/Q22); the 3/3 metric overstates what was
   verified.
4. **`ingest.py` classifier over-assigns `fard` in salah** — matches
   `\bfard\b` in titles like "Joining the farḍ prayer". Harmless today (empty
   item pool, correctly skipped) but a source-level bug.
5. **Context-dependent MCQ options** — items whose qualifying condition lives
   in the chapter heading read ambiguously once lifted out ("Partaking the
   pre-dawn meal").
6. **The `is_junk_char` range fix above** — deliberately not fixed; ride
   along with any future re-ingest.

---

## 2026-09-02 — off Anthropic: a provider abstraction, and layer 1 rebuilt

**Why:** the Anthropic credit balance was running out. The decision was to make
the provider a setting rather than a rewrite, and to default to Azure OpenAI.

### What changed

- **`src/ai_fiqh/llm.py`** (new) — three backends behind one interface:
  `complete()` and `parse()`, plus `cited_complete()` on Anthropic only.
  Selected by `AI_FIQH_LLM_PROVIDER` (`azure` default | `ollama` | `anthropic`).
  Every provider failure is raised as one of four classes (`LLMConfigError`,
  `LLMUnavailable`, `LLMOverloaded`, `LLMError`) so `app.py` no longer imports a
  vendor SDK to catch its exceptions.
- **`anthropic` moved to the `cloud` dependency group**; `openai` and `ollama`
  are now first-class deps. The Anthropic path is *kept, not deleted* — it is
  the only implementation with API-native citations, so it is what the
  replacement gets measured against.
- **Azure parameter dialects are discovered, not tabulated.** Deployment names
  are arbitrary, so nothing about the underlying model can be read off one.
  `_AzureClient._adapt` learns `max_completion_tokens` vs `max_tokens`, and
  whether `temperature` is accepted, from the 400 it gets back — once per
  process. This is deliberately not a model table, which would go stale.

### Layer 1 of §1.7 was rebuilt, and it is weaker than what it replaced

API-native citations were a *structural* guarantee: a citation object could not
name a document that was not supplied. No other provider offers this. The
replacement numbers the excerpts and asks for `[n]` markers, which `qa.resolve_
markers` maps back to chunks — **and reports the markers that resolve to
nothing**, because a model writing prose absolutely can cite `[9]` over five
excerpts. Silently dropping those would have hidden exactly the fabrication the
layer exists to catch.

Two honest downgrades, recorded so they are not rediscovered later:

1. **Provenance is asked for, not guaranteed.** The check is now code rather
   than API structure. It catches out-of-range markers; it cannot catch a
   marker that points at a real excerpt that does not support the claim.
2. **`cited_text` no longer holds a source span.** A native citation quotes the
   source; a marker is just a number. `_sentence_around` attaches the *claim*
   instead — which is arguably more useful to a reader checking an answer, but
   it is not the same field and the UI shows something different now.

`prompts.QA_SYSTEM` is unchanged; the citation rules are appended separately as
`QA_SYSTEM_MARKERS` / `QA_PROMPT_VERSION_MARKERS = "qa-v2-markers"` so the two
stay diffable and eval runs stay attributable.

### Context budgeting (`qa.fit_to_context`)

New, and needed only because small windows are now reachable. A no-op on Azure
(128k) and Anthropic (200k). Measured worst cases on this corpus:

| Path | Worst case | 8k model |
|---|---|---|
| Ordinary Q&A | ~4,800 tok | fits |
| §2.2 enumeration (whole section + top-5) | ~9,500 tok | 6/14 chunks kept |
| `generate_flashcards` on Hajj rituals | 22,346 chars | trimmed to 20,324 |

Trimming is **from the tail**, which is load-bearing: `chunks` arrives in
priority order in both modes (reranked-best-first, or whole-section-first for an
enumeration), so the tail is the least valuable context in either case and the
top hit is never separated from its §1.3 polarity siblings. At least one chunk
always survives. Anything dropped is reported on `Answer.context_dropped` rather
than swallowed.

**Ollama's `num_ctx` defaults to 2,048**, not the model's window. Without setting
it explicitly a retrieval context that fits gemma2's 8,192 would have been
truncated in half, silently, with nothing in the response to say so.

### Measured: gemma2:9b fails the polarity case that retrieval solved

One live question through `ollama/gemma2:9b`, 4,833 input tokens, 67s:

> **Q: Does laughing aloud break wudu?** — gemma2 quoted the *non*-nullifiers
> list back verbatim and concluded "This source does not mention laughing
> aloud."

That is **wrong**, and the failure is entirely the model's. Retrieval did its
job: both sides of the polarity group were in context, and
`017-those-things-which-nullify-wudu` (p17) item 11 reads "The loud laughing of a
mature person, whilst awake, in a prayer consisting of…". The model read one side
of the contrasting pair and ignored the other — **precisely the failure §1.3's
group expansion exists to make impossible, reintroduced at the model layer.**

Worth stating plainly: the polarity design guarantees both sides *reach* the
model. It cannot make the model read them. Layers 2 and 4 are code and held fine
here; layers 1 and 3 are only ever as good as the model reading them.

### Eval harness: judging is now a separate concern

`--provider` and `--judge-provider` were added, and every run is stamped with
both. By default the judge *is* the model under test, which is self-grading; the
harness now prints a warning when they match. `citation validity` counts both
halves of layer 4 (`unverified_pages` **and** `unresolved_markers`).

### Still open after this pass

- **Azure is not yet configured or tested.** `AZURE_OPENAI_ENDPOINT`,
  `_API_KEY` and `_DEPLOYMENT` are unset; the Azure path is written and compiles
  but has never made a live call. Everything verified below was verified through
  Ollama, which exercises the identical non-native-citation code path.
- **The eval has not been re-run on any new provider.** The 40/40 in
  `eval/results/20260804-103342.json` is `claude-opus-5` with native citations
  and `qa-v1`. It does **not** carry over. Do not quote it for Azure.
- **`MIN_RERANK_SCORE = 0.74` is unaffected** — it gates on the Voyage reranker,
  which did not change. Retrieval is untouched by this pass.
- **The "no self-verification instruction" omission in `QA_SYSTEM` was
  calibrated on Opus 5** and is not established for any other model. A weaker
  model may need the instruction Opus made redundant. Flagged in `prompts.py`.
- **Azure subscription is a corporate Vodafone tenant** (`vf.group.architecture.
  chatgptpoc.openai-cha.dev`) while this is a personal project. Acceptable-use
  question, raised with the user 2026-09-02.

## 2026-09-03 — false abstentions: the gate, not the retriever

Two real questions came back "the book does not cover this" when the book covers
both. Investigated in `notebooks/explore.ipynb` §8.

### Diagnosis: retrieval succeeded, the gate discarded the result

| Question | rerank | verdict |
|---|---|---|
| "How to do Iqama" | 0.6484 | ABSTAIN |
| "does bleeding from the mouth break wudu?" | 0.7383 | ABSTAIN — **by 0.0017** |

The notebook's first read was "no chunks are being retrieved". That is not what
happened: `qa.answer` returns `chunks=[]` on a gate abstention by construction,
before it assembles any context. Retrieval had in fact **succeeded** — for the
bleeding question the exactly correct chunk,
`017-those-things-which-nullify-wudu`, was ranked **#1 by the reranker**. This is
a layer-2 false abstention, not a retrieval failure, and the distinction matters
because it points at an entirely different fix.

### The embedding model was ruled out, with measurement

The hypothesis on the table was a better embedding model or a higher
`EMBED_DIM`. Both were rejected:

| Stage | recall@20 on the 24 labelled golden questions |
|---|---|
| BM25 | 24/24 |
| **Dense** | **24/24** — correct chunk in the top 3 every time, median rank 1 |
| Fused | 24/24 |

There is no headroom to buy. And more decisively, `rerank-2.5` is a
**cross-encoder**: it re-scores raw text and never touches the embeddings, so
`EMBED_MODEL` and `EMBED_DIM` cannot move the number the gate compares against,
except by changing which candidates reach it — and the right candidate already
does. **Do not spend a re-embed on this.**

### The gate threshold sits in a dead zone

Every score `rerank-2.5` returns is an exact multiple of 1/512. Since
`0.74 × 512 = 378.88`, the representable values either side are **0.738281** and
**0.740234**, and *no score can ever land between them*. So:

- the effective gate is **0.740234**, not the 0.74 written in `index.py`;
- the bleeding question's 0.7383 is **the highest score that can possibly fail**.

Worth setting `MIN_RERANK_SCORE` to a representable value so the constant means
what it says.

### What was fixed

**1. Dead and missing aliases (`normalize.py`).** `iqama` was not in the BM25
vocabulary — the corpus spells it `iqamah` — so BM25 scored zero on the query's
only content word and ranked on `how`/`to`/`do`, handing the top four slots to
"Chapter on how to perform the rituals of Hajj". Added 25 romanisation variants,
mostly the dropped-trailing-h class.

Auditing that turned up **three pre-existing dead aliases** — targets that appear
**zero** times in the corpus, so the expansion could never match:

| alias target | occurrences | corpus actually uses |
|---|---|---|
| `rakah` ← rakat, rakaat, rakah | **0** | `rakaah` (143) |
| `sahur` ← suhoor, sehri | **0** | `suhur` (2) |
| `sai` ← saee, saiy | **0** | folds to `say`, indistinguishable from the English verb |

`rakah` is the serious one: it is among the most common terms in the salah
chapters and every query mentioning rak'ahs was being expanded with a term that
could not match. `sai` now expands to `safa marwah` instead, since expanding to
`say` would add 58 hits of "he said". **Every alias target is now verified
present in `text_folded`** — a test worth keeping.

**2. Grey-band query rewriting (`qa.py`).** When the top score lands in
`[REWRITE_FLOOR=0.60, gate)`, one cheap call rewrites the *question* into a
better *search query* and retrieval runs again; the rephrasing is kept only if it
actually scores higher. Measured:

| Question | before | after | outcome |
|---|---|---|---|
| How to do Iqama | 0.6484 | **0.8203** | answered correctly |
| does bleeding from the mouth break wudu? | 0.7383 | **0.8320** | answered correctly |
| Does the Iqama involve repitition | 0.6094 | 0.7383 | **still abstains** — lands exactly on the dead-zone value |

**This does not weaken layer 2.** The model sees the question and nothing else —
no corpus text — its output never reaches the user, and the abstention is still
decided in code on a reranker score afterwards. What it does change is that a
grey-band question now costs one model call before abstaining.

### The safety check that mattered

Rewriting could launder an out-of-scope question into an answerable one. The
prompt therefore forbids dropping scope qualifiers, and this was tested against
all 16 should-abstain questions:

- rewrite fired on **2/16**; the other 14 were below the 0.60 floor or above the
  gate, and the 8 out-of-scope ones never triggered a call at all;
- **both rewrites preserved the madhhab name** ("…in the Shafi'i school",
  "Hanbali fiqh…") — the safety property held;
- **1** (Q35, Hanbali) crossed the gate and reached the model, which declined the
  comparative and gave the Hanafi position — the designed §2.4 behaviour, scored
  `declined` rather than `leaked`.

Net effect: one question moved from a layer-2 code abstention to a layer-3 model
decline. That is a real shift of load from code to model and should be watched,
but it is the behaviour §2.4 specifies.

### Still open

- **The eval has not been re-run** with rewriting on. `--no-rewrite` exists
  precisely so it can be measured against its own absence; do that before
  claiming it helps.
- **`MIN_RERANK_SCORE` is unchanged at 0.74.** Observed should-abstain max is
  0.7188 and the failing legitimate question is 0.7383, so a gate anywhere in
  (0.7188, 0.7383] would fix the third question and still exclude every
  should-abstain case measured. **But that is re-fitting on the same saturated
  set the tracker already warns about**, and it is a safety-relevant constant —
  it should move on evidence from a hardened golden set, not on one example.
- **These two questions are not in the golden set.** They are the first
  real-world false abstentions found, and they belong in it — see the standing
  "harden the golden set" item, for which this is now direct evidence.

## 2026-09-04 — golden set to 42, eval re-run, and an Azure content-filter wall

### Golden set: 40 -> 42

The two real-world false abstentions from 2026-09-03 are now in the set, worded
**exactly as they were typed**, typo and all — the vagueness is the thing under
test and "fixing" the wording would delete the test.

| id | category | question | why it is here |
|---|---|---|---|
| Q41 | Polarity trap | "does bleeding from the mouth break wudu?" | scored 0.7383, abstained by 0.0017 with the correct chunk at rerank #1 |
| Q42 | Straightforward covered | "How to do Iqama" | scored 0.6484; `iqama` was absent from the BM25 vocabulary |

Categories are now uneven (9/9/8/8/8). That is deliberate: the alternative is
padding two categories with questions nobody has a reason to ask.

### Eval: the rewrite works, and the headline number cannot show it

Three runs on `azure/gpt-5.4`, self-graded (no independent judge available with
Anthropic credit exhausted — the harness prints the warning every run).

| metric | rewrite OFF | rewrite ON | ON, 2nd run |
|---|---|---|---|
| behaviour | 39/42 | 39/42 | **40/42** |
| ruling agreement | 23/26 | 23/26 | 24/26 |
| **false abstention** | **3/26** | **1/26** | **1/26** |
| **recall@context** | **24/26** | **26/26** | **26/26** |
| **polarity accuracy** | **8/9** | **9/9** | **9/9** |
| abstention (should abstain) | 16/16 | 16/16 | 16/16 |
| citation validity | 42/42 | 42/42 | 42/42 |

**Read the headline number sceptically.** Rewrite-off and rewrite-on both scored
39/42, which looks like no effect. It is not: the rewrite fixed exactly the two
questions it targeted (Q41 0.7383 -> 0.8320, Q42 0.6484 -> 0.8203, both
`abstained` -> `agrees`) while two *unrelated* questions, Q23/Q24, regressed
`agrees` -> `incomplete` in the same run.

Q23/Q24 were never rewritten, and their retrieval was byte-identical between
runs (`top_score=0.8555`, `recall=True`). They regressed purely from generation
nondeterminism — **`gpt-5.4` accepts `temperature=0.0` and is still not
deterministic.** Two identical rewrite-on runs scored 39/42 and 40/42.

So: **this harness has ±1–2 questions of run-to-run noise, and a single run
cannot resolve a 2-question difference.** Judge any future change on the
retrieval-determined metrics (false abstention, recall@context), which are
stable, not on `behaviour`. Repeat runs before believing a small delta.

The rewrite's cost is bounded and low: it fired on 4/42, and out-of-scope
questions sit below the 0.60 floor so they never spend a call.

### ⚑ Azure's content filter blocks part of the Book of Purity

`Q01 "What are the obligatory (fard) acts of ghusl?"` failed in **all three
runs**, deterministically, and it is not a retrieval or model problem:

```
019-things-which-do-not-necessitate-ghusl  p19  ->  400 content_filter, sexual: medium
```

Isolated chunk by chunk. It is a plain classical enumeration of the ten things
that do not require ritual bathing — madhi, wadi, a dream without wetness — and
Azure's default filter rejects **the whole prompt** on it. `param` is `prompt`,
so it is the *book's own text* being refused, not the question and not the
answer.

Two things make this worse than one failing question:

1. **It is the negative half of the `ghusl-necessitate` polarity pair.** Group
   expansion (§1.3) deliberately includes it, so the filter blocks a chunk the
   architecture is specifically designed to always supply.
2. **It presents as a false abstention.** `llm.LLMContentFiltered` now degrades
   to the §1.7 refusal path, so the user sees "the book does not cover this"
   about a question the book answers on p19.

**The right fix is the filter policy, not code.** Azure content filters are
configurable per deployment, and a modified filter can be requested for the
resource. This is a corporate Vodafone tenant, so it likely needs the platform
team rather than a portal toggle.

Code mitigations were considered and **not** implemented, because each trades
away something the design promises: retrying without the blocked chunk would
silently drop one side of a polarity pair, which is the exact failure §1.3
exists to prevent. Abstaining loudly is the safer default until the policy is
changed. Anything built here must make the degradation visible rather than
quietly answer from half a contrast set.

### Two bugs fixed while running this

- **Thread race in `AzureClient._adapt`** — the parameter-dialect discovery was
  not thread-safe. With 4 workers, the thread that lost the race found the
  dialect already corrected, concluded there was nothing left to adapt, and
  raised a hard failure on a request that would now succeed. Cost Q02/Q03/Q04 of
  the first run. Now under a lock, and "retry" is returned whenever the
  rejection names a parameter the client knows how to fix, regardless of which
  thread fixed it.
- **`run_eval` aborted the whole run on one question's exception.** A 42-question
  run died at question ~30 and discarded every result already computed. Failures
  are now recorded as `error` rows, counted as failures, and reported.

### Still open

- **No independent judge.** Every number above is self-graded by the model under
  test. `--judge-provider` exists; it needs a second provider with credit.
- **`MIN_RERANK_SCORE` still 0.74**, still in the 1/512 dead zone (effective
  0.740234). Q33 ("Do Shafi'i scholars consider bleeding to break wudu?") now
  rewrites to 0.7266 and correctly still abstains, so the band between
  should-abstain and answerable has narrowed further — evidence for leaving the
  threshold alone until the golden set is harder.
- **Q23/Q24 (`zakah-obligation` variants) omit "a free Muslim"** from the
  conditions about half the time. Intermittent, so it is a generation-quality
  issue, not a retrieval one.

## Environment

- **Path:** `/Users/rifatmahammod/Developer/personal-projects/ai-fiqh`
- **Python:** 3.13.0 via `uv` venv at `.venv/`, all deps installed
- **Git:** work lands directly on `main`. Remote is
  `git@github.com:RifatM97/ai-fiqh.git`; local `main` has run ahead of
  `origin/main` before, so check `git status -sb` before assuming it is pushed.
  **2026-08-04 push status, checked directly:** `git fetch origin` +
  `git rev-list --left-right --count origin/main...main` reports `0 0` —
  `origin/main` already carries `f751045`, i.e. all three recent commits
  (`6838e0a`, `f4d6663`, `f751045`) are on the remote. `gh auth status` shows
  `RifatM97` as the active account with `push: true` on the repo
  (`gh api repos/RifatM97/ai-fiqh --jq .permissions` → admin/push/pull all
  true). This **contradicts an earlier note** that push was blocked because
  `gh` was authenticated as `rifat-mahammod_voda` (pull-only) and the loose
  SSH key wasn't registered — that must have been resolved (account switch
  and/or a push) between whenever that note was written and this check.
  Nothing left to do here; recorded so a future stale-blocker note doesn't
  recur.
  **2026-08-05 push status:** three more commits landed (`b4c59f2`,
  `c949e87`, `5849ff9`). A `git fetch` attempted mid-pass failed with "Could
  not resolve host: github.com" (no network at that moment) — recorded as a
  reminder that push state should be *re-checked*, not trusted from a stale
  note. A retry of `git fetch origin` later in the same pass succeeded, and
  `git rev-list --left-right --count origin/main...main` reported `0 0` —
  `origin/main` is at `5849ff9`, i.e. all three commits above are confirmed
  on the remote as of this check. What is **not** pushed: `app.py`
  (untracked) and the `normalize.py` / `pyproject.toml` / `uv.lock` changes
  underneath it — all still uncommitted on disk, see *2026-08-05* above.
- **Credentials:** `.env` at repo root holds `ANTHROPIC_API_KEY` and
  `VOYAGE_API_KEY`. Gitignored, loaded via `python-dotenv`. An SSH keypair was
  found loose in the repo root on 2026-08-03 (never committed) and moved to
  `~/.ssh/`; `.gitignore` now blocks `ssh-key*`, `*.pem`, `id_rsa*`,
  `id_ed25519*` (landed in `f4d6663`). Verified 2026-08-04: repo root is clean,
  no `ssh-key*` present.
- **Voyage billing:** payment method added 2026-08-03 — see *Throttling* below.
  Throttle constants raised the same day; cold rebuild now **8.3 seconds**
  (down from ~9 minutes).
- **Anthropic credits:** exhausted mid-session on 2026-08-04, topped up same
  day. This is why `notebooks/explore.ipynb` is committed with §1–3 verified
  on the post-top-up run but §4–7 only verified on an earlier execution —
  worth re-running end-to-end once touched again, since the two executions
  aren't guaranteed to agree byte-for-byte.
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

### Open questions added 2026-08-03 — both resolved, same day

1. ~~**`MIN_RERANK_SCORE` is an unvalidated placeholder (0.40).**~~ **Resolved —
   measured, not guessed: raised to 0.74.** Distribution over the labelled
   golden set (n=40): answerable (n=24) min 0.769 / median 0.863 / max 0.926;
   should-abstain (n=16) min 0.262 / median 0.598 / max 0.719. Cleanly
   separable; any threshold in (0.719, 0.769) splits them. Set to 0.74,
   mid-band. Rationale is written into the code comment. See *Golden eval set*
   below for the caveats — they matter more than the number.
2. ~~**Golden set has no `chunk_id` labels**~~ **Resolved — labelled.**
   `expected_chunk_ids`, `should_abstain`, `variant_group` added to all 40
   questions via `eval/label_chunks.py` (propose) → `eval/apply_labels.py`
   (commit). See *Golden eval set* below.

### Open questions added 2026-08-04

1. **The golden set is saturated (40/40, zero failures) and can no longer
   detect a regression or distinguish a better config from a worse one.**
   Needs harder cases: questions the book answers ambiguously or tersely,
   near-miss polarity pairs. Hajj is the obvious gap given known thin
   coverage (2 of the 30 original xlsx questions, and revision-mode
   distractor coverage there is already flagged thin — see *Known limitation
   to revisit*).
2. **`sawm-kaffarah` variant group (Q20–22) conflates two different things.**
   Q21/Q22 are a genuine spelling-variant pair and agree at top-1; Q20 omits
   the Arabic term entirely and retrieves a different (still correct within
   top-5) chunk. The 3/3 variant-agreement metric doesn't distinguish these.
   Either split Q20 into its own group or redefine the metric on recall
   rather than top-1.
3. ~~**`retrieve.py` still not started**~~ — **resolved 2026-08-04: dropped, not
   deferred.** Its two planned functions already exist as `Retriever.search`
   (§2.2's `search_fiqh`) and `Retriever.get_section`, both in `index.py`, and
   both are used directly by `qa.py` and `revision.py`. A wrapper module would
   add a layer without adding behaviour. Removed from research.md §3.4; the
   design in §2.2 stands unchanged, only its file layout did.

---

## Category coverage for Zakah and Hajj — drafted 2026-08-07, **awaiting review**

**Where it stands:** `docs/category-overrides.draft.json` exists, is annotated,
and is **not wired into anything**. Nothing was committed. It needs a human with
fiqh knowledge to check it before any of it is applied.

### The problem

Practice questions (MCQs) draw distractors from the legal category the book
files a ruling under, so a distractor is guaranteed wrong. Zakah has **zero**
category-labelled chunks and Hajj has **one**, so the UI hides both books from
the MCQ tab. Flashcards cover them instead, but that is a workaround, not a fix.

### What the investigation actually found — the first fix was wrong

The obvious approach, a per-chunk `CATEGORY_OVERRIDES` map keyed by chunk id,
**does not work and would be actively harmful.** Chunk
`132-the-book-of-hajj-p1` contains *both* "The wājibāt of Hajj" and "The sunan
of Hajj": labelling it `wajib` would file the sunan items as wājib, and a
distractor drawn from that pool would be secretly correct. That is precisely the
failure §2.3's whole design exists to make impossible.

The real cause is upstream. Hajj and Zakah **do** have proper category headings
in the body — "The wājibāt of Hajj" (p134), "The sunan of Hajj" (p135),
"Conditions for Hajj to become compulsory" (p132) and so on. The printed ToC
simply does not list them, and `ingest.py` uses ToC entries as its only
segmentation anchors, so those headings never became chunk boundaries and
everything below them landed as `general`.

**So the fix is an `EXTRA_HEADINGS` list in `ingest.py`** — headings to treat as
segmentation anchors alongside the ToC — not a category map. The draft is
therefore a table of *headings*, not chunk ids.

### The draft

33 candidate headings found book-wide (not just Hajj — taharah and salah gain
several too). 29 to review, 4 proposed for dropping. Each row carries `page`,
`kitab`, `heading` verbatim, `auto_category` (what `classify_category` returns
today), `proposed_category`, `confidence`, and a `note`.

`items_following` is **approximate and marked as such** — the counter runs past
section ends, reporting ~31 for a list of 18.

### Two real bugs in `ingest.py` this surfaced

Neither causes harm *today*, because the affected chunks are `general` and
nothing draws from them. **Both become live the moment this table is applied**,
so fix them in the same pass.

1. **`mustaḥab` is classified as `makruh`.** `_CATEGORY_RULES` puts
   `\bmakruh|\bdisliked\b|\bmustahab` in one rule mapping to `"makruh"`. Those
   are opposites. Hits "Mustaḥab prayer times" (p32) and "Things which are
   mustaḥab for a fasting person" (p116). If either fed a distractor pool, the
   MCQ would offer a *recommended* act as the *disliked* answer — a
   confidently-taught error of exactly the kind §2.3 is built to prevent.
   **Highest-risk item in this whole table.**
2. **"Conditions that necessitate its fulfilment" is classified as
   `nullifier`**, because the rule matches `\bnecessitate`. Hits p102 (sawm) and
   p133 (hajj); both are condition lists, i.e. `shurut`.

### Three judgement calls left for the reviewer

1. **The four "drop" rows** — "It is mustaḥab" (a prose fragment), "Fulfilment
   of a vow prior to its condition", "The sunnah shroud for a male consists of",
   "When is it sunnah to do Rafʿ al-Yadain?". Read as lists of garments or
   occasions rather than rulings of one category. Confirm or reinstate.
2. **`Arkān of ṣalāh` (p37)** — currently `general`; a new `arkan` category is
   proposed. Would need a `CONTRAST_SETS` entry in `revision.py`, and whether
   arkān-vs-wājib is a fair exam contrast is a fiqh call.
3. **The three AUTO-WRONG rows above** — confirm the corrections before they
   become distractor pools.

### Cascade to plan for

Applying this re-runs ingest, which rewrites `index/chunks.json`, rebuilds
`embeddings.npy` (~8s), and may shift the golden set's `expected_chunk_ids`. The
eval must be re-run afterwards and the labels re-checked.

**Bundle it with the `is_junk_char` boundary fix** (see that item under *Still
open*), which needs the same re-ingest — one cascade instead of two.

---

## Golden eval set (2026-08-03, rebalanced + labelled same day)

Written by hand. Started as `eval/golden-eval-set.xlsx` — 30 content questions
(Purity 10 / Salah 11 / Zakah 4 / Fasting 3 / Hajj 2) with free-text answers,
all of them the "straightforward covered" category. Then extended to the full
§4 design as `eval/golden-eval-set.json`, and later **rebalanced to exactly 40
questions, 8 per category** (supersedes the earlier 10/9/8/7/6 breakdown):

| Category | Count |
|---|---|
| Straightforward covered | 8 |
| Polarity trap | 8 |
| Transliteration variant | 8 |
| Out of scope | 8 |
| Cross-madhhab bait | 8 |

Every question now also has a hand-written `reference_answer`. Note: the
abstention categories' reference answers are refusal texts, not content.

### Labelling — closes the `chunk_id` gap

Two scripts, deliberately split so proposing (heuristic) and committing
(human decision) are never the same run:

- **`eval/label_chunks.py`** — runs each question through the retriever with
  `expand=False`, ranks candidates by `answer_coverage` (the fraction of the
  *reference answer's* content words present in the chunk), flags weak
  proposals, and prints the gate calibration table. Writes
  `eval/label-proposals.json`, commits nothing.
- **`eval/apply_labels.py`** — writes reviewed labels into the golden set.
  Only ever adds fields; raises `SystemExit` if any hand-written key would be
  lost. Wrote a one-time `eval/golden-eval-set.json.bak` backup first
  (`eval/` is untracked, so there was no git safety net).

Three fields added: `expected_chunk_ids` (24 labelled, the 16 abstention-only
questions get `[]`), `should_abstain`, and `variant_group` (3 groups linking
the transliteration triples: wudu-fard Q17-19, sawm-kaffarah Q20-22,
zakah-obligation Q23-24).

**One manual override — worth recording as a lesson.** Q05 ("how many arkan
of salah"): the coverage heuristic picked `041-the-sunan-of-salah-p0` (cov
0.75) because the reference answer's words — standing, rukū', sujūd — also
occur in the 51-item sunan list. The real definition is in
`036-the-prerequisites-of-salah-and-its-components-p1` (p37): "Arkān of ṣalāh
/ Four from the above-mentioned twenty seven are arkān." Retrieval had ranked
it #1 (rr=0.852) all along — **the heuristic was wrong and retrieval was
right.** Q14 was flagged weak (cov 0.40) but verified correct: the book
phrases it as "recite a portion of the Qur'an which he hasn't memorised
looking into the muṣḥaf" (p52), so low coverage was vocabulary mismatch, not
a bad label.

### `MIN_RERANK_SCORE` calibration

See resolved open question #1 above for the numbers and threshold. **The
caveats matter more than the number:** the threshold is fitted on the same
set the eval reports against, so this is not evidence of generalisation; the
margin is only 0.05; and the tightest negatives are cross-madhhab bait (max
0.719) — predictably, since the *topic* is in the book and only the madhhab
is not. Those are exactly the cases §1.7 layers 1/3/4 exist for, and a clean
gate number is not a reason to weaken them.

### Current measured scores

**Gate 40/40, retrieval recall@5 24/24, transliteration variants 3/3 groups
agree.**

Recall@5 needs a methodological caveat, not a flat "100%": it was initially
circular, because labels were derived from retrieval's own top-5, so a chunk
retrieval never surfaced could never have become a label. Cross-checked by
recomputing `answer_coverage` over **all 177 chunks** independently of
retrieval. That produced 4 disagreements (Q05, Q08, Q14, Q15), and all four
were verified to be heuristic errors rather than missed labels — e.g.
`008-the-rulings-pertaining-to-leftover-water-sur` genuinely mentions
cats/su'r while `012-istinja` does not; `129-chapter-on-sadaqat-alfitr-p0` is
plainly right for a Sadaqat al-Fitr question. The sweep found no chunk that
retrieval had missed, so recall@5 = 24/24 now has genuine independent
support — though the coverage heuristic is itself a weak oracle.

**Remaining gap:** the 30 original xlsx content questions are only partially
folded in — the JSON carries 8 "Straightforward covered", so 20 of the 30
still exist only in `eval/golden-eval-set.xlsx`. Still worth merging in; they
are real hand-written Fiqh content and re-deriving them is not free.

---

## Known issues in `index.py` — found by the smoke test, fixed 2026-08-03

Both bugs below are now fixed — see *Retrieval results* above for the fix
description and verification. Left here for the record of what the smoke test
caught:

1. ~~`expand_groups()` mutates the `Scored` objects it was handed.~~ The final
   renumbering loop wrote `s.rank` in place, and `trace.reranked` held the same
   objects, so inspecting the RERANK stage after expansion showed corrupted
   ranks (observed: `1, 3, 4, 4, 5`). Fixed with `dataclasses.replace` so
   expansion returns copies.
2. ~~A chunk that was already reranked gets relabelled `group-expansion`~~ if a
   sibling pulled it in first, and inherited the trigger's score instead of its
   own. Same root cause, fixed in the same pass.

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
seconds. They are the only thing making it slow.

**Raised, same day.** `EMBED_BATCH_TOKENS` 8,000 → 32,000; `TPM_BUDGET` 10,000 →
1,000,000; `MIN_REQUEST_INTERVAL` 20.0 → 0.0. Cold rebuild measured at **8.3
seconds** (3 batches, 89,210 tokens), down from ~9 minutes. The retry/backoff
path and checkpointing were deliberately kept as-is — they cost nothing when
unused, and the failure mode they guard against (losing a build to one 429)
doesn't go away just because it's rarer now.

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

**The build order in [research.md §6](research.md) is complete — all ten
steps done.** Both credentials are in place, retrieval works, the
`expand_groups()` trace bugs are fixed, the throttle constants are raised,
the golden set exists, is rebalanced, is labelled, and has a measured gate
threshold, the Q&A pipeline is built and passing eval (with the caveats
above), the exploration notebook exists, revision mode is built, and the
Streamlit UI (§2.1) is built and verified end-to-end via `AppTest`. Nothing
in the original design remains unbuilt.

1. ~~**Revision mode (§2.3)**~~ — **done 2026-08-04**, committed as `b4c59f2`
   plus the deck builder. MCQs with corpus-drawn distractors, flashcards, and
   `build_deck` giving Zakah and Hajj the coverage the MCQ path structurally
   cannot. See *Revision mode* above.
2. ~~`retrieve.py`~~ — **dropped**, committed as `5849ff9`. See resolved open
   question 3 above.
3. ~~**Streamlit UI (`app.py`, §2.1)**~~ — **done 2026-08-05, uncommitted.**
   Three tabs (Ask / Practice questions / Flashcards), verified via `AppTest`
   against real API calls including an abstention path and a 529 error path.
   The prerequisite noted here previously — bab titles carry an unmappable
   Private Use Area glyph (the Hajj visiting chapter ends in the unrendered
   SAW glyph) — is now handled: the new `display_title()` helper in
   `normalize.py` strips only Private Use Area / control codepoints at the
   display boundary, leaving
   `get_section`'s exact-match key untouched. See *2026-08-05* above for
   design points and the bug found by testing. **Still to do:** commit it,
   along with `normalize.py` and the `pyproject.toml` `ui` dependency group
   it depends on.

What remains is quality work against what already exists, not new
components:

- **Commit and push the uncommitted UI work** — `app.py`, `normalize.py`
  (`display_title`), `pyproject.toml` / `uv.lock` (`ui` dependency group).
  Currently only on disk.
- **Merge the remaining 20 of the 30 xlsx content questions** into the JSON
  golden set (see *Golden eval set* above).
- **Harden the golden set** — it's saturated (see *Open questions added
  2026-08-04*); add harder/ambiguous/terse cases, prioritising Hajj.
- **`sawm-kaffarah` variant group** — split Q20 out or redefine the metric on
  recall (see *Open questions added 2026-08-04*).
- **⚑ Zakah/Hajj MCQ coverage — draft ready, awaiting your fiqh review.**
  `docs/category-overrides.draft.json`, 29 rows to check plus 4 proposed for
  dropping. Not wired into anything. Read *Category coverage for Zakah and
  Hajj* above before starting: the per-chunk approach was investigated and
  **rejected as harmful**, the fix is an `EXTRA_HEADINGS` list in `ingest.py`,
  and it surfaced two live-on-application `ingest.py` bugs — one of which
  classifies `mustaḥab` as `makruh`, i.e. exactly backwards.
- **`ingest.py`'s `fard`-in-salah over-match** — source-level classifier bug,
  currently harmless (see *Still open*, 2026-08-05 above). Same file as the two
  bugs above; worth fixing in the same pass.
- **Context-dependent MCQ options** whose qualifying condition lives in the
  chapter heading (see *Still open*, 2026-08-05 above).
- **`is_junk_char`'s `0x250` boundary** — documented defect, deliberately
  deferred to the next re-ingest (see *2026-08-05* above). **Bundle this with
  the category-override work above** — both require a re-ingest, and the
  cascade (rewrite `chunks.json` → rebuild `embeddings.npy` → re-check the
  golden set's `expected_chunk_ids` → re-run the eval) is worth paying once
  rather than twice.
- Update the stale routing line in `.claude/CLAUDE.md` (still says
  "Automatically span between sub-agents depending on what the user asking" —
  flagged 2026-07-27, still not fixed).

> **Commit state, updated 2026-08-05.** `6838e0a` (2026-08-03 14:37) captured
> `index.py` in full — including the `expand_groups()` fixes and the raised
> throttle constants — plus the golden set, the first tracker pass, and
> `python-dotenv`. `f4d6663` (2026-08-03 15:47) added the gate calibration
> (`MIN_RERANK_SCORE` 0.40 → 0.74), the labelling scripts, the labelled
> golden set, `.gitignore` credential patterns, and the SSH-key move.
> `f751045` (2026-08-04 17:14) added the Q&A pipeline, eval harness, eval
> run, and notebook — see the *2026-08-04* section above. All three are
> confirmed present on `origin/main`. **Updated 2026-08-05:** three more
> commits landed — `b4c59f2` "Add revision mode: MCQs with corpus-drawn
> distractors, and flashcards", `c949e87` "Add flashcard decks so Zakah and
> Hajj have revision coverage", `5849ff9` "Drop retrieve.py from the design;
> record why rather than deleting it". Re-verified directly this pass (after
> an earlier `git fetch` in the same pass failed on a network error):
> `origin/main` is at `5849ff9`, matching local `main` — all six commits to
> date are confirmed on the remote. Uncommitted on disk right now:
> `src/ai_fiqh/app.py` (new — the Streamlit UI) and `src/ai_fiqh/normalize.py`
> (modified — adds `display_title()`), plus the `pyproject.toml` / `uv.lock`
> changes that add the `ui` dependency group. `revision.py` / `schemas.py`,
> previously in progress, are now committed as part of `b4c59f2` / `c949e87`.

### Known limitation to revisit

Category classification leaves **132 of 177 chunks as `general`** — correct, since most
sections are topical ("Tayammum", "Chapter of Witr") rather than category-shaped. But
the corpus-drawn distractor scheme (research.md §2.3) only works where real categories
exist (fard / wajib / sunnah / adab / makruh ≈ 25 chunks). That is enough for MCQs on
the core enumerations, which is the point — but revision coverage will be uneven across
the book, and Hajj especially is thin.
