"""Generate a held-out set of naturally-phrased questions, for §1.7 layer-2 work.

Why this exists: the 42-question golden set was written by someone who had read
the book, so its questions speak the book's language -- and `MIN_RERANK_SCORE`
was calibrated against them. Measurement on 2026-09-04 showed the gate rewards
that resemblance directly: three phrasings of one question, retrieving the *same*
chunk at the *same* rank, scored 0.7070 / 0.7227 / 0.7734, and only the one that
echoed the book's own wording ("immediately after another") cleared the gate.

**This is an approximation and should be labelled as one.** The tracker's option 4
asks for questions written by people who have not read the book, which is not
something a model that has read it can produce. What this script *can* do is
enforce the property that actually matters, in code rather than by intention:

    a variant is kept only if it shares LESS of the source passage's vocabulary
    than the original golden question does

So the set is not "what a real user would ask". It is "phrasings measurably
further from the book's wording than the golden set is", which is the axis the
gate turns out to be sensitive to. Treat scores on it as a robustness probe, not
as a user study.

Run:  uv run python eval/make_natural_set.py            # propose, write nothing
      uv run python eval/make_natural_set.py --write    # write the set
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_fiqh import llm  # noqa: E402
from ai_fiqh.index import load_chunks  # noqa: E402
from ai_fiqh.revision import _content_words  # noqa: E402

GOLDEN_PATH = ROOT / "eval" / "golden-eval-set.json"
OUT_PATH = ROOT / "eval" / "natural-phrasing-set.json"

VARIANTS_WANTED = 3
MAX_TOKENS = 800

_SYSTEM = """\
You rewrite questions about Islamic religious practice the way ordinary people \
actually type them.

You will be given one question that was written by someone holding a classical
Hanafi manual, so it uses the book's own vocabulary. Rewrite it several ways as
different people might ask the same thing — someone who has never opened the
book, does not know the technical terms, and is typing into a search box.

Rules:

- **Keep the meaning exactly.** The correct answer must not change. Do not make
  it broader, narrower, or about a different case.
- **Avoid the classical term where a plain English word exists.** Prefer
  "washing before prayer" to "wuduh", "breaks" to "nullifies", "must" to
  "wajib", "call to prayer" to "adhan".
- **Vary the shape**, not just the words: a direct question, a practical "what if
  I…" situation, and a terse fragment of the kind people actually type.
- Do not add a greeting, a preamble, or an explanation. Just the questions.

Never change which madhhab or which subject is being asked about. If the question
names a school of law, that name stays."""


class Variants(BaseModel):
    variants: list[str] = Field(description="Rewritten questions, meaning unchanged")


def overlap(question: str, source: str) -> float:
    """Share of the question's content words that appear in the source passage.

    The same stemmed, stopword-filtered measure revision mode uses for grounding,
    reused deliberately: it is already calibrated on this corpus's prose, and a
    second similarity measure would be a second thing to explain.
    """
    q = _content_words(question)
    return len(q & _content_words(source)) / len(q) if q else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--write", action="store_true", help="write the set (default: dry run)")
    ap.add_argument("--limit", type=int)
    args = ap.parse_args()

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    chunks = {c["id"]: c for c in load_chunks()}
    client = llm.get_client()

    # Abstention questions are excluded on purpose. Their correct behaviour does
    # not depend on retrieval clearing the gate, so a paraphrase of one measures
    # nothing about phrasing sensitivity -- and rewording "what do the Shafi'i
    # say" risks quietly removing the very qualifier that makes it out of scope.
    answerable = [q for q in golden if not q["should_abstain"] and q["expected_chunk_ids"]]
    if args.limit:
        answerable = answerable[: args.limit]

    out: list[dict] = []
    rejected = 0
    print(f"{len(answerable)} answerable questions, asking for {VARIANTS_WANTED} variants each\n")

    for item in answerable:
        source = chunks[item["expected_chunk_ids"][0]]["text_raw"]
        base = overlap(item["question"], source)
        try:
            got = client.parse(
                _SYSTEM,
                f"Question: {item['question']}\n\n"
                f"Write {VARIANTS_WANTED} rewritten versions.",
                Variants,
                max_tokens=MAX_TOKENS + client.thinking_reserve,
            )
        except llm.LLMError as exc:
            print(f"  {item['id']}  SKIPPED — {str(exc)[:90]}")
            continue

        print(f"  {item['id']}  golden overlap {base:.0%}  {item['question'][:58]}")
        for n, variant in enumerate(got.variants[:VARIANTS_WANTED], 1):
            variant = variant.strip()
            score = overlap(variant, source)
            # The whole point of the set: keep it only if it is measurably further
            # from the book's wording than the golden question already was.
            keep = bool(variant) and score < base
            mark = "keep" if keep else "drop"
            print(f"      [{mark}] {score:>4.0%}  {variant[:70]}")
            if not keep:
                rejected += 1
                continue
            out.append({
                "id": f"{item['id']}N{n}",
                "source_id": item["id"],
                "category": item["category"],
                "question": variant,
                "expected_behavior": item["expected_behavior"],
                "reference_answer": item["reference_answer"],
                "expected_chunk_ids": item["expected_chunk_ids"],
                "should_abstain": False,
                "golden_overlap": round(base, 4),
                "variant_overlap": round(score, 4),
            })

    kept = len(out)
    print(f"\nkept {kept}, dropped {rejected} (overlap not below the golden question's)")
    if kept:
        deltas = [o["golden_overlap"] - o["variant_overlap"] for o in out]
        print(f"mean overlap reduction: {sum(deltas)/len(deltas):.1%}")

    if not args.write:
        print(f"\ndry run — pass --write to save to {OUT_PATH.relative_to(ROOT)}")
        return
    OUT_PATH.write_text(json.dumps(out, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"\nwritten -> {OUT_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
