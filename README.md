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
AZURE_OPENAI_DEPLOYMENT=gpt-5.4    # your deployment's name, not the model id

# Recommended on Azure — see "When the content filter refuses"
AI_FIQH_LLM_FALLBACK_PROVIDER=ollama
```

`AZURE_OPENAI_DEPLOYMENT` is whatever you named the deployment in the Azure
portal, so nothing about the underlying model can be read off it — which is why
`llm.AzureClient` discovers the parameter dialect from the API's own 400 rather
than from a lookup table. The Ollama fallback needs the model pulled once:

```bash
ollama pull gemma4:12b
```

Embeddings build themselves on first search (~8s) and cache to `index/`.

## Models in use

**Answers come from `gpt-5.4` on Azure OpenAI, with `gemma4:12b` on local Ollama
as a fallback.** Retrieval is Voyage AI in every configuration — `voyage-4-large`
for embeddings, `rerank-2.5` for reranking — and is unaffected by the generation
provider.

Generation goes through `src/ai_fiqh/llm.py`, so the provider is a setting rather
than a rewrite:

| `AI_FIQH_LLM_PROVIDER` | Model | Context | Role |
|---|---|---|---|
| `azure` *(default)* | **`gpt-5.4`** | 128k | Primary. Strict JSON-schema structured output. Optional `AZURE_OPENAI_API_VERSION`, `_CONTEXT_TOKENS`. |
| `ollama` | **`gemma4:12b`** | 16k as configured | Content-filter fallback, and the free/offline option. `OLLAMA_MODEL`, `OLLAMA_HOST`, `OLLAMA_CONTEXT_TOKENS`, `OLLAMA_THINK`, `OLLAMA_THINKING_RESERVE`. |
| `anthropic` | `claude-opus-5` | 200k | The original, and the only one with API-native citations. Kept runnable so the replacement has something to be measured against. Needs `uv sync --group cloud`. |

Two things worth knowing about the local model. **`gemma4:12b` is a reasoning
model** — it returns `thinking` alongside `content`, and Ollama's `num_predict`
caps the two together, so a ceiling sized for the answer alone comes back empty.
Thinking is therefore **off by default**: with it on, the reserve it needs takes
roughly 3,000 tokens off the excerpt budget, which is enough to start trimming
retrieved context. Set `OLLAMA_THINK=1` to re-enable it and raise
`OLLAMA_CONTEXT_TOKENS` to match.

And its advertised context is 262k, which is **not** what to request: at 32k an
18GB machine went to 9.6GB of swap. 16,384 leaves ~10,900 tokens for excerpts
against a measured worst case of 9,500, at half the KV cache.

## Run

```bash
uv run streamlit run src/ai_fiqh/app.py
```

Three tabs: **Ask**, **Practice questions** (MCQs), **Flashcards**.

## Evaluation

```bash
uv run python eval/run_eval.py            # scored run against the golden set
uv run python eval/run_eval.py --sweep    # gate threshold trade-off, no API calls
uv run python eval/run_eval.py --provider ollama --judge-provider azure
```

**42 hand-written questions** across five categories: covered, polarity traps,
transliteration variants, out-of-scope, and cross-madhhab bait. A companion set of
**77 naturally-phrased questions** (`eval/natural-phrasing-set.json`) probes
phrasing robustness — the golden set was written by someone holding the book, so
it shares the book's vocabulary and understates false abstention roughly
fourfold.

Every run is stamped with the model *and* the judge, because scores are not
comparable across either. By default the judge is the model under test, which is
self-grading — the harness warns when that happens, and `--judge-provider` gives
an independent grader.

Current run on `azure/gpt-5.4` (`eval/results/two-level-gate.json`): **41/42
behaviour, 0/26 false abstention, 16/16 abstention, 26/26 recall@context.**
The one failure is a **bad reference answer**, not a model error — `Q01` says
three farḍ acts of ghusl where the book enumerates eleven.

**Read `docs/PROJECT_TRACKER.md` before trusting any number here.** Scores are
self-graded, the golden set is saturated, and the older 40/40 in
`eval/results/20260804-103342.json` was measured on `claude-opus-5` with
API-native citations and does not carry over.

## Layout

| Path | |
|---|---|
| `src/ai_fiqh/index.py` | Hybrid retrieval — BM25 + dense, RRF, rerank, group expansion |
| `src/ai_fiqh/llm.py` | Provider abstraction — Azure OpenAI, Ollama, Anthropic |
| `src/ai_fiqh/qa.py` | Q&A pipeline and the four abstention layers |
| `eval/gate_signals.py` | Which retrieval signal layer 2 should gate on — measured |
| `eval/make_natural_set.py` | Builds the phrasing-robustness question set |
| `src/ai_fiqh/revision.py` | MCQs and flashcards |
| `src/ai_fiqh/prompts.py` | System prompts, versioned |
| `notebooks/explore.ipynb` | Inspect retrieval stage by stage |
| `docs/research.md` | Why it is built this way |
| `docs/PROJECT_TRACKER.md` | State, decisions, and known defects |

## Caveats

Single book, single madhhab, `ʿibādāt` only. Zakāh and Hajj have no MCQ
coverage — neither has the category metadata a distractor needs — so they get
flashcards instead.

The abstention behaviour this project exists for is **model-dependent**. It has
been measured on `claude-opus-5` and `gpt-5.4`; `gemma4:12b` passes the polarity
case that `gemma2:9b` failed but still fabricated a supporting clause that the
citation check cannot catch, which is why it is the fallback and not the primary.
Layer 4 holds regardless — it compares cited pages against supplied pages. Layers
1 and 3 are only as good as the model reading them, and **layer 2 is a retrieval
signal, not a judgement**: it abstains below 0.60 and flags rather than abstains
above it. Re-run the eval after changing provider or model.

**Not a substitute for a qualified scholar.**
