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
printf 'ANTHROPIC_API_KEY=...\nVOYAGE_API_KEY=...\n' > .env
uv run python -m ai_fiqh.ingest    # one-time: PDF -> index/chunks.json
```

Embeddings build themselves on first search (~8s) and cache to `index/`.

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

**A plausible answer is indistinguishable from a correct one.** Four independent
defences, two of them code that keeps working when the model has a bad day:
API-native citations, a confidence gate that abstains *before* any model call,
an authority-boundary prompt, and a check that every page cited was actually in
context.

Practice questions use the same idea: wrong answers are drawn from the book's
own categories, so a distractor is wrong because the book files it elsewhere —
never because the model judged it wrong.

## Evaluation

```bash
uv run python eval/run_eval.py            # scored run against the golden set
uv run python eval/run_eval.py --sweep    # gate threshold trade-off, no API calls
```

40 hand-written questions across five categories: covered, polarity traps,
transliteration variants, out-of-scope, and cross-madhhab bait. Current run is
40/40 — but read `docs/PROJECT_TRACKER.md` before trusting that number: the set
is saturated and the gate threshold was fitted on it.

## Layout

| Path | |
|---|---|
| `src/ai_fiqh/index.py` | Hybrid retrieval — BM25 + dense, RRF, rerank, group expansion |
| `src/ai_fiqh/qa.py` | Q&A pipeline and the four abstention layers |
| `src/ai_fiqh/revision.py` | MCQs and flashcards |
| `src/ai_fiqh/prompts.py` | System prompts, versioned |
| `notebooks/explore.ipynb` | Inspect retrieval stage by stage |
| `docs/research.md` | Why it is built this way |
| `docs/PROJECT_TRACKER.md` | State, decisions, and known defects |

## Caveats

Single book, single madhhab, `ʿibādāt` only. Zakāh and Hajj have no MCQ
coverage — neither has the category metadata a distractor needs — so they get
flashcards instead. **Not a substitute for a qualified scholar.**
