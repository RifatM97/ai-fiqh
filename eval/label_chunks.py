"""Propose `expected_chunk_ids` for the golden set, for human review.

Recall@5 needs to know which chunk actually contains each answer. Labelling 40
questions by hand against 177 chunks is the kind of job that gets done badly, so
this proposes labels and shows the evidence for each -- but writes nothing.
Review the output, then run `apply_labels.py`.

The proposal is *not* "whatever retrieval returned" -- that would make recall@5
vacuously 100%. Candidates come from retrieval, but they are ranked by how much
of the reference answer's vocabulary actually appears in the chunk text. A chunk
that retrieval loved but that does not contain the answer scores near zero.

Run: uv run python eval/label_chunks.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ai_fiqh.index import Retriever, load_chunks
from ai_fiqh.normalize import fold

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "eval" / "golden-eval-set.json"
OUT_PATH = ROOT / "eval" / "label-proposals.json"

# These two categories are correct only when the system abstains, so they have no
# expected chunk. Their retrieval scores are still worth collecting: they are the
# negative half of the MIN_RERANK_SCORE calibration (§1.7 layer 2).
ABSTAIN_CATEGORIES = {"Out of scope", "Cross-madhhab bait"}

# Words too common in this corpus to be evidence of anything.
STOPWORDS = set(
    """the a an of to in is it and or for on with that this these those which not no
    are be if by as at from does do their there they he she his her its one two
    when what who how whether must may can will would should shall than then also
    any all some such other into upon over under after before during while""".split()
)


def content_terms(text: str) -> set[str]:
    folded = fold(text)
    return {w for w in re.findall(r"[a-z]+", folded) if len(w) > 2 and w not in STOPWORDS}


def answer_coverage(reference: str, chunk_text: str) -> float:
    """Fraction of the reference answer's content words present in the chunk."""
    ref = content_terms(reference)
    if not ref:
        return 0.0
    return len(ref & content_terms(chunk_text)) / len(ref)


def main() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    retriever = Retriever(load_chunks(), verbose=False)

    proposals = []
    for q in golden:
        abstain = q["category"] in ABSTAIN_CATEGORIES
        # No group expansion: labels should name the chunk that holds the answer,
        # not the siblings that §1.3 drags along at query time.
        trace = retriever.search(q["question"], expand=False)

        scored = [
            {
                "chunk_id": s.id,
                "bab": s.chunk["bab"],
                "pages": f"{s.chunk['page_start']}-{s.chunk['page_end']}",
                "rerank_score": round(s.score, 4),
                "answer_coverage": round(
                    answer_coverage(q["reference_answer"], s.chunk["text_raw"]), 3
                ),
            }
            for s in trace.reranked
        ]
        best = max(scored, key=lambda c: c["answer_coverage"], default=None)

        proposals.append(
            {
                "id": q["id"],
                "category": q["category"],
                "question": q["question"],
                "reference_answer": q["reference_answer"],
                "abstain_expected": abstain,
                "proposed_chunk_ids": [] if abstain or not best else [best["chunk_id"]],
                "top_rerank_score": round(trace.top_score, 4),
                "candidates": scored,
            }
        )

    OUT_PATH.write_text(json.dumps(proposals, indent=2, ensure_ascii=False), encoding="utf-8")

    # --- review output ------------------------------------------------------
    for p in proposals:
        flag = ""
        if not p["abstain_expected"]:
            cov = max((c["answer_coverage"] for c in p["candidates"]), default=0.0)
            if cov < 0.5:
                flag = f"  <-- WEAK ({cov:.2f}) needs a human"
        print(f"\n{p['id']} [{p['category']}]{flag}")
        print(f"  Q: {p['question']}")
        for c in p["candidates"][:3]:
            mark = "*" if c["chunk_id"] in p["proposed_chunk_ids"] else " "
            print(
                f"   {mark} cov={c['answer_coverage']:.2f} rr={c['rerank_score']:.3f} "
                f"p{c['pages']:<7} {c['chunk_id']}"
            )

    # --- gate calibration ---------------------------------------------------
    pos = [p["top_rerank_score"] for p in proposals if not p["abstain_expected"]]
    neg = [p["top_rerank_score"] for p in proposals if p["abstain_expected"]]
    print("\n" + "=" * 70)
    print("MIN_RERANK_SCORE calibration (§1.7 layer 2)")
    print("=" * 70)
    print(f"  answerable (n={len(pos)}): min {min(pos):.3f}  median {sorted(pos)[len(pos)//2]:.3f}  max {max(pos):.3f}")
    print(f"  should-abstain (n={len(neg)}): min {min(neg):.3f}  median {sorted(neg)[len(neg)//2]:.3f}  max {max(neg):.3f}")
    if min(pos) > max(neg):
        print(f"  SEPARABLE -- any threshold in ({max(neg):.3f}, {min(pos):.3f}) splits them cleanly")
    else:
        print(f"  OVERLAP of {max(neg) - min(pos):.3f} -- the gate alone cannot separate these;")
        print("  layers 1/3/4 have to carry the abstention cases in the overlap band")
    print(f"\nproposals written -> {OUT_PATH}")


if __name__ == "__main__":
    main()
