# AI-Fiqh

Grounded Hanafi fiqh Q&A and exam revision over a single book —
*Nur al-Idah* by Abu al-Ikhlas Hasan al-Shurunbulali, covering `ʿibādāt`:
purity, prayer, fasting, zakāh and hajj.

**It answers only from that book, and declines everything else.** Ask what the
Shāfiʿī school holds, or how inheritance is divided, and it says so rather than
answering from what the model happens to know. That refusal is the point of the
project, not a limitation of it.

## Setup

```bash
uv sync --group ui                 # add --group notebook for the explorer
uv run python -m ai_fiqh.ingest    # one-time: PDF -> index/chunks.json
```

Retrieval always needs `VOYAGE_API_KEY` (embeddings and the reranker).
Generation needs whichever provider you select. Put both in `.env`:

```bash
VOYAGE_API_KEY=...

AI_FIQH_LLM_PROVIDER=azure         # azure | ollama | anthropic
AZURE_OPENAI_ENDPOINT=https://<resource>.openai.azure.com/
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_DEPLOYMENT=<your deployment name>

# Optional, and recommended on Azure — see "When the content filter refuses"
AI_FIQH_LLM_FALLBACK_PROVIDER=ollama
```

Embeddings build themselves on first search (~8s) and cache to `index/`.

## Model providers

Generation goes through `src/ai_fiqh/llm.py`, so the provider is a setting rather
than a rewrite. Retrieval is unaffected either way — it is Voyage in all three.

| `AI_FIQH_LLM_PROVIDER` | Needs | Context | Notes |
|---|---|---|---|
| `azure` *(default)* | `AZURE_OPENAI_ENDPOINT`, `_API_KEY`, `_DEPLOYMENT` | 128k | Strict JSON-schema structured output. Optional `_API_VERSION`, `_CONTEXT_TOKENS`. |
| `ollama` | `ollama serve` + a pulled model | 16k as configured | Free and offline, and **no content filter**. `OLLAMA_MODEL` (default `gemma4:12b`), `OLLAMA_HOST`, `OLLAMA_CONTEXT_TOKENS`, `OLLAMA_THINKING_RESERVE`. |
| `anthropic` | `ANTHROPIC_API_KEY`, `uv sync --group cloud` | 200k | The only one with API-native citations; kept for comparison. |

### When the content filter refuses

Azure's content filter rejects part of this corpus. `Nur al-Idah`'s Book of
Purity covers ghusl after intercourse, menstruation and istihadah in the clinical
register a 17th-century jurist would use, and Azure scores
`019-things-which-do-not-necessitate-ghusl` (p19) as `sexual: medium` and refuses
**the whole prompt** — the book's own text, not the question and not the answer.
Configuring a custom filter did not lift it.

Left alone, that surfaces as the system telling a user *"Nur al-Idah does not
appear to address this"* about a ruling printed on p19 — a false statement about
the book, which costs more credibility than it saves.

So `AI_FIQH_LLM_FALLBACK_PROVIDER` names a second provider used for **exactly**
that case:

```
Azure returns a content-filter refusal  ->  retry the same prompt on Ollama
anything else (rate limit, outage, 400) ->  fail normally
```

It is deliberately narrow. Falling back on transient errors would mask outages,
and falling back on a genuine model refusal would defeat §1.7 layer 3. The
substitute model is named on the answer (`Answer.answered_by`) and shown as a
warning in the UI, because a reader is entitled to know a different, weaker model
produced it. Retrieval and every citation check are unchanged.

Two constraints, enforced at construction: both providers must agree on citation
style (otherwise one receives a prompt built for the other's scheme), and the
context budget uses the **smaller** of the two windows, since the prompt is built
before anyone knows which provider will answer it.

**Citations work differently off Anthropic.** Anthropic returns citations as
structural objects that cannot point outside the documents supplied. Everywhere
else the excerpts are numbered and the model cites `[n]` — so `qa.resolve_markers`
maps each marker back to its chunk and *reports the ones that resolve to
nothing*. A model citing `[9]` over five excerpts is caught rather than silently
dropped. The guarantee moved from the API into code.

**Small windows are budgeted, not hoped for.** `qa.fit_to_context` trims the
excerpt set to what the selected model can hold, dropping from the tail so the
top hit and its polarity siblings survive. This is a no-op on Azure and
Anthropic; on an 8k local model it only bites on the enumeration path, where a
whole section is merged in and the Hajj rituals chapter alone is ~5,600 tokens.
Anything dropped is reported on the answer, never swallowed.

## Run

```bash
uv run streamlit run src/ai_fiqh/app.py
```

Three tabs: **Ask**, **Practice questions** (MCQs), **Flashcards**.

## How it avoids being confidently wrong

Two failure modes matter here, and both are handled structurally rather than by
asking the model nicely.

**Opposite rulings sit next to each other.** "Things which nullify wuḍūʾ" (p17)
and "things which do *not*" (p18) are near-identical to an embedding model and
opposite in law. Contrasting sections share a `group_id`, and retrieval always
returns the whole group — so the model reads both sides and cannot pick the
wrong one, because it never picks.

**Retrieval can succeed while the score fails.** The confidence gate compares a
reranker score against a threshold, and a vaguely-worded question scores low even
when the correct passage is ranked first. So a score in the grey band just below
the gate buys one rephrasing: the question — never the corpus — goes to the model,
which returns a better *search query*, and retrieval runs again. The abstention
decision is still made in code, on a score, after that returns. The rephrasing is
shown in the UI, because the book was searched for something other than what was
typed. Questions scoring below the floor are abstained on without spending a call.

**A plausible answer is indistinguishable from a correct one.** Four independent
defences, three of them code that keeps working when the model has a bad day:
resolved citations, a confidence gate that abstains *before* any model call, an
authority-boundary prompt, and a check that every page and excerpt cited was
actually in context.

Practice questions use the same idea: wrong answers are drawn from the book's
own categories, so a distractor is wrong because the book files it elsewhere —
never because the model judged it wrong.

## Evaluation

```bash
uv run python eval/run_eval.py            # scored run against the golden set
uv run python eval/run_eval.py --sweep    # gate threshold trade-off, no API calls
uv run python eval/run_eval.py --provider ollama --judge-provider azure
```

40 hand-written questions across five categories: covered, polarity traps,
transliteration variants, out-of-scope, and cross-madhhab bait. Every run is
stamped with the model *and* the judge, because scores are not comparable across
either. By default the judge is the model under test, which is self-grading —
the harness prints a warning when that happens, and `--judge-provider` is how you
get an independent grader.

The 40/40 in `eval/results/20260804-103342.json` was measured on `claude-opus-5`
with native citations. **It does not carry over to another provider** — read
`docs/PROJECT_TRACKER.md` before trusting any number here: the set is saturated
and the gate threshold was fitted on it.

## Layout

| Path | |
|---|---|
| `src/ai_fiqh/index.py` | Hybrid retrieval — BM25 + dense, RRF, rerank, group expansion |
| `src/ai_fiqh/llm.py` | Provider abstraction — Azure OpenAI, Ollama, Anthropic |
| `src/ai_fiqh/qa.py` | Q&A pipeline and the four abstention layers |
| `src/ai_fiqh/revision.py` | MCQs and flashcards |
| `src/ai_fiqh/prompts.py` | System prompts, versioned |
| `notebooks/explore.ipynb` | Inspect retrieval stage by stage |
| `docs/research.md` | Why it is built this way |
| `docs/PROJECT_TRACKER.md` | State, decisions, and known defects |

## Caveats

Single book, single madhhab, `ʿibādāt` only. Zakāh and Hajj have no MCQ
coverage — neither has the category metadata a distractor needs — so they get
flashcards instead.

The abstention behaviour this project exists for is **model-dependent**, and the
only provider it has been measured on is `claude-opus-5`. Layers 2 and 4 are code
and hold regardless; layers 1 and 3 are only as good as the model reading them.
Re-run the eval after changing provider or model.

**Not a substitute for a qualified scholar.**
