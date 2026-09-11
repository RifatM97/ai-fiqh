"""Which retrieval signal should §1.7 layer 2 gate on?

`run_eval.py --sweep` sweeps a threshold over one signal: the top reranked score.
This asks the prior question -- whether that is the right signal at all -- because
measurement on 2026-09-04 showed it is not separable: the lowest genuinely
answerable score observed (0.7070) sits below the highest should-abstain score
(0.7266), so no threshold on it can be correct.

A cross-encoder score is an *ordinal* judgement on a query-dependent scale. The
candidates below are attempts to remove that scale dependence -- margins and
ratios within one query's own distribution, which a synonym swap should not move.

The operating point is chosen the way the project's priorities imply, not by
maximising accuracy: **answering an out-of-scope question is the expensive error**,
so each signal is scored by the false-abstention rate it costs at the strictest
threshold that still abstains on every should-abstain question. Lower is better.

    uv run python eval/gate_signals.py --collect   # retrieval only, caches scores
    uv run python eval/gate_signals.py             # analyse the cache, no API calls
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ai_fiqh.index import MIN_RERANK_SCORE, Retriever  # noqa: E402

GOLDEN_PATH = ROOT / "eval" / "golden-eval-set.json"
NATURAL_PATH = ROOT / "eval" / "natural-phrasing-set.json"
CACHE_PATH = ROOT / "eval" / "gate-scores.json"

TOP_N = 5  # how many reranked scores each signal may look at


# --- candidate signals -------------------------------------------------------
# Each takes the reranked score vector, best first, and returns one number where
# higher means "more likely answerable".


def sig_top(s: list[float]) -> float:
    """The signal in production today. Absolute, so phrasing moves it."""
    return s[0]


def sig_margin_second(s: list[float]) -> float:
    """Gap to the runner-up."""
    return s[0] - s[1] if len(s) > 1 else s[0]


def sig_margin_tail(s: list[float]) -> float:
    """Gap between the best and the mean of the rest."""
    return s[0] - statistics.mean(s[1:]) if len(s) > 1 else s[0]


def sig_ratio_tail(s: list[float]) -> float:
    """Best over the mean of the rest. Scale-free by construction."""
    rest = statistics.mean(s[1:]) if len(s) > 1 else s[0]
    return s[0] / rest if rest > 0 else 0.0


def sig_zscore(s: list[float]) -> float:
    """How many standard deviations the best score stands above the field."""
    if len(s) < 3:
        return 0.0
    sd = statistics.pstdev(s)
    return (s[0] - statistics.mean(s)) / sd if sd > 1e-9 else 0.0


def sig_top2_mean(s: list[float]) -> float:
    """Mean of the best two.

    Included because §1.3 group expansion deliberately puts both halves of a
    polarity pair near the top, so the *pair* scoring well is the signal -- and a
    margin-based measure reads that same situation as ambiguity.
    """
    return statistics.mean(s[:2]) if len(s) > 1 else s[0]


def sig_top_plus_margin(s: list[float]) -> float:
    """Absolute strength and distinctiveness together, equally weighted."""
    return sig_top(s) + sig_margin_tail(s)


SIGNALS = {
    "top (current)": sig_top,
    "margin to #2": sig_margin_second,
    "margin to tail": sig_margin_tail,
    "ratio to tail": sig_ratio_tail,
    "z-score of top": sig_zscore,
    "mean of top 2": sig_top2_mean,
    "top + margin": sig_top_plus_margin,
}


# --- collection --------------------------------------------------------------


def collect() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    natural = (
        json.loads(NATURAL_PATH.read_text(encoding="utf-8"))
        if NATURAL_PATH.exists()
        else []
    )
    r = Retriever(verbose=False)
    rows = []
    items = [("golden", q) for q in golden] + [("natural", q) for q in natural]
    print(f"collecting reranked scores for {len(items)} questions (retrieval only)")
    for n, (origin, q) in enumerate(items, 1):
        # `expand=False`: group expansion happens after reranking and cannot
        # change `reranked`, so this saves work without changing the numbers.
        trace = r.search(q["question"], expand=False)
        rows.append({
            "id": q["id"],
            "origin": origin,
            "category": q["category"],
            "question": q["question"],
            "should_abstain": q["should_abstain"],
            "scores": [round(s.score, 6) for s in trace.reranked[:TOP_N]],
            "top_chunk": trace.reranked[0].id if trace.reranked else None,
            "expected": (q.get("expected_chunk_ids") or [None])[0],
        })
        if n % 20 == 0:
            print(f"  {n}/{len(items)}")
    CACHE_PATH.write_text(json.dumps(rows, indent=2, ensure_ascii=False) + "\n",
                          encoding="utf-8")
    print(f"written -> {CACHE_PATH.relative_to(ROOT)}")


# --- analysis ----------------------------------------------------------------


def strictest_safe_threshold(pos: list[float], neg: list[float]) -> tuple[float, int]:
    """The best threshold that still abstains on every should-abstain question.

    "Safe" means no negative passes. Among thresholds satisfying that, pick the
    one admitting the most positives. Returns (threshold, false abstentions).
    """
    if not neg:
        return min(pos), 0
    # Any threshold above every negative is safe; the lowest such is the best.
    threshold = max(neg) + 1e-9
    return threshold, sum(1 for p in pos if p < threshold)


def auc(pos: list[float], neg: list[float]) -> float:
    """Probability a random answerable question outscores a random abstain one."""
    if not pos or not neg:
        return float("nan")
    wins = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def analyse() -> None:
    rows = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    rows = [r for r in rows if r["scores"]]
    neg_rows = [r for r in rows if r["should_abstain"]]

    for scope, label in (("all", "golden + natural"), ("golden", "golden only")):
        pos_rows = [r for r in rows
                    if not r["should_abstain"]
                    and (scope == "all" or r["origin"] == "golden")]
        print(f"\n{'=' * 78}")
        print(f"{label}:  {len(pos_rows)} answerable vs {len(neg_rows)} should-abstain")
        print("=" * 78)
        print(f"{'signal':<18}{'AUC':>7}{'safe thr':>11}{'false abst':>13}   rate")
        print("-" * 78)
        scored = []
        for name, fn in SIGNALS.items():
            pos = [fn(r["scores"]) for r in pos_rows]
            neg = [fn(r["scores"]) for r in neg_rows]
            thr, bad = strictest_safe_threshold(pos, neg)
            scored.append((bad, -auc(pos, neg), name, thr, len(pos)))
            print(f"{name:<18}{auc(pos, neg):>7.3f}{thr:>11.4f}"
                  f"{bad:>9}/{len(pos):<3}{100*bad/len(pos):>7.1f}%")
        best = min(scored)
        print(f"\n  best by false-abstention rate: {best[2]!r} "
              f"({best[0]}/{best[4]} = {100*best[0]/best[4]:.1f}%)")

    # the specific cases that motivated this
    print(f"\n{'=' * 78}\nthe measured failures, under each signal")
    print("=" * 78)
    watch = [r for r in rows if r["id"] in ("Q41", "Q42")
             or "consecutive" in r["question"].lower()
             or "straightaway" in r["question"].lower()]
    neg_all = {n: [f(r["scores"]) for r in neg_rows] for n, f in SIGNALS.items()}
    for r in watch:
        print(f"\n  {r['id']:<8} {r['question'][:62]}")
        for name, fn in SIGNALS.items():
            v = fn(r["scores"])
            thr = max(neg_all[name]) + 1e-9
            print(f"      {name:<18} {v:>8.4f}  vs safe thr {thr:>7.4f}  "
                  f"{'PASS' if v >= thr else 'abstain'}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--collect", action="store_true", help="re-run retrieval and cache scores")
    args = ap.parse_args()
    if args.collect:
        collect()
    if not CACHE_PATH.exists():
        raise SystemExit("no cache — run with --collect first")
    print(f"\ncurrent production gate: MIN_RERANK_SCORE = {MIN_RERANK_SCORE} on 'top'")
    analyse()


if __name__ == "__main__":
    main()
