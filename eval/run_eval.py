"""Score the Q&A pipeline against the golden set (docs/research.md §4).

Five metrics, all computable without a human — plus the one the smoke tests
couldn't cover: whether the answer is actually *right*.

    behaviour          did it answer when it should, decline when it shouldn't
    recall@context     was the labelled chunk in what reached the model
    citation validity  did every page it named appear in context (§1.7 layer 4)
    ruling agreement   does the answer match the hand-written reference answer
    variant agreement  do transliteration variants of one question behave alike

Two things make the scoring less obvious than it looks.

**Abstention is not silence.** On a cross-madhhab question the designed
behaviour (§2.4) is to decline the comparative part *and* still give the Hanafi
ruling on the underlying topic. Scoring a boolean `abstained` flag marks all
eight of those as failures. So an LLM judge decides whether the out-of-scope
part leaked, not whether the model stayed quiet.

**A fluent wrong ruling looks exactly like a right one.** The failure mode here
is silent, so ruling agreement is judged against the reference answer rather
than assumed from a clean citation trace.

Run:  uv run python eval/run_eval.py
      uv run python eval/run_eval.py --sweep          # gate threshold, no model calls
      uv run python eval/run_eval.py --category "Polarity trap" --workers 2
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ai_fiqh import prompts, qa  # noqa: E402
from ai_fiqh.index import MIN_RERANK_SCORE, Retriever  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "eval" / "golden-eval-set.json"
RESULTS_DIR = ROOT / "eval" / "results"

JUDGE_MODEL = "claude-opus-5"
JUDGE_EFFORT = "medium"

ABSTAIN_CATEGORIES = {"Out of scope", "Cross-madhhab bait"}


# --- judges ------------------------------------------------------------------
# Structured outputs, not citations — the two are mutually exclusive (§1.7), and
# a grader needs a parseable verdict far more than it needs citation spans.


class RulingVerdict(BaseModel):
    verdict: Literal["agrees", "contradicts", "incomplete", "abstained"]
    reason: str


class AbstentionVerdict(BaseModel):
    verdict: Literal["declined", "leaked"]
    reason: str


_RULING_JUDGE = """\
You are grading a Hanafi fiqh answer against a reference answer taken from the \
source book. Judge only whether the *ruling* matches. Wording, ordering, extra \
context, and added scholar-referral notes are irrelevant.

- `agrees` — the ruling matches the reference, including which side of a
  yes/no it lands on. An answer that adds correct detail beyond the reference
  still agrees.
- `contradicts` — the ruling differs from the reference on any substantive
  point, or lands on the opposite side of a yes/no.
- `incomplete` — the reference gives an enumeration and the answer omits
  members of it, without contradicting anything.
- `abstained` — the answer declines to give a ruling at all.

Be strict about `agrees` on enumerations: a missing item is `incomplete`."""

_ABSTENTION_JUDGE = """\
You are checking whether an answer respected a scope boundary. Its only source \
is *Nur al-Idah*, a Hanafi manual covering 'ibadat (purity, prayer, fasting, \
zakah, hajj).

The correct behaviour is to decline the out-of-scope part. It is **not** a
failure to also state what the Hanafi source says about an underlying topic
that the book does cover — that is the designed behaviour.

- `declined` — refused to supply the out-of-scope ruling. It may still give the
  Hanafi position on a related in-scope topic, and it may explain the boundary.
- `leaked` — supplied the substantive out-of-scope ruling anyway: stated what
  another madhhab holds, or gave a ruling on a subject outside 'ibadat, as
  though it were sourced.

The question is only whether the out-of-scope content was supplied."""


def _judge(client, system: str, user: str, schema: type[BaseModel]):
    response = client.messages.parse(
        model=JUDGE_MODEL,
        max_tokens=4_000,
        output_config={"effort": JUDGE_EFFORT},
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=schema,
    )
    return response.parsed_output


def judge_ruling(client, question: str, reference: str, answer_text: str) -> RulingVerdict:
    return _judge(
        client,
        _RULING_JUDGE,
        f"QUESTION\n{question}\n\nREFERENCE ANSWER\n{reference}\n\n"
        f"ANSWER UNDER TEST\n{answer_text}",
        RulingVerdict,
    )


def judge_abstention(client, question: str, answer_text: str) -> AbstentionVerdict:
    return _judge(
        client,
        _ABSTENTION_JUDGE,
        f"QUESTION\n{question}\n\nANSWER UNDER TEST\n{answer_text}",
        AbstentionVerdict,
    )


# --- per-question run --------------------------------------------------------


@dataclass
class Row:
    id: str
    category: str
    question: str
    should_abstain: bool
    variant_group: str | None
    abstained: bool
    abstain_reason: str | None
    behaviour_ok: bool
    verdict: str
    verdict_reason: str
    recall: bool | None
    n_citations: int
    unverified_pages: list[int] = field(default_factory=list)
    top_score: float = 0.0
    enumeration: bool = False
    seconds: float = 0.0
    answer: str = ""


def run_one(item: dict, retriever: Retriever, client, gate: float) -> Row:
    started = time.time()
    ans = qa.answer(item["question"], retriever=retriever, client=client, gate=gate)

    recall = None
    if item["expected_chunk_ids"]:
        recall = item["expected_chunk_ids"][0] in {c["id"] for c in ans.chunks}

    if item["should_abstain"]:
        # A gate abstention is a decline by construction — no need to pay a judge.
        if ans.abstained:
            verdict, reason = "declined", f"gate abstention ({ans.abstain_reason})"
        else:
            v = judge_abstention(client, item["question"], ans.text)
            verdict, reason = v.verdict, v.reason
        behaviour_ok = verdict == "declined"
    else:
        if ans.abstained:
            verdict, reason = "abstained", f"false abstention ({ans.abstain_reason})"
        else:
            v = judge_ruling(client, item["question"], item["reference_answer"], ans.text)
            verdict, reason = v.verdict, v.reason
        behaviour_ok = verdict == "agrees"

    return Row(
        id=item["id"],
        category=item["category"],
        question=item["question"],
        should_abstain=item["should_abstain"],
        variant_group=item.get("variant_group"),
        abstained=ans.abstained,
        abstain_reason=ans.abstain_reason,
        behaviour_ok=behaviour_ok,
        verdict=verdict,
        verdict_reason=reason,
        recall=recall,
        n_citations=len(ans.citations),
        unverified_pages=ans.unverified_pages,
        top_score=round(ans.trace.top_score, 4) if ans.trace else 0.0,
        enumeration=ans.enumeration,
        seconds=round(time.time() - started, 1),
        answer=ans.text,
    )


# --- reporting ---------------------------------------------------------------


def report(rows: list[Row], gate: float) -> dict:
    answerable = [r for r in rows if not r.should_abstain]
    abstaining = [r for r in rows if r.should_abstain]
    polarity = [r for r in rows if r.category == "Polarity trap"]

    by_group: dict[str, list[Row]] = {}
    for r in rows:
        if r.variant_group:
            by_group.setdefault(r.variant_group, []).append(r)
    consistent = sum(
        1 for g in by_group.values() if len({r.verdict for r in g}) == 1
    )

    metrics = {
        "behaviour": (sum(r.behaviour_ok for r in rows), len(rows)),
        "ruling agreement": (
            sum(r.verdict == "agrees" for r in answerable), len(answerable)
        ),
        "abstention (should abstain)": (
            sum(r.verdict == "declined" for r in abstaining), len(abstaining)
        ),
        "polarity accuracy": (
            sum(r.verdict == "agrees" for r in polarity), len(polarity)
        ),
        "recall@context": (
            sum(1 for r in answerable if r.recall), len(answerable)
        ),
        "citation validity": (
            sum(1 for r in rows if not r.unverified_pages), len(rows)
        ),
        "variant agreement": (consistent, len(by_group)),
    }

    print(f"\n{'=' * 66}")
    print(f"prompt {prompts.QA_PROMPT_VERSION} | model {qa.MODEL} | "
          f"effort {qa.EFFORT} | gate {gate}")
    print("=" * 66)
    for name, (num, den) in metrics.items():
        pct = f"{100 * num / den:5.1f}%" if den else "    --"
        print(f"  {name:<28} {num:>3}/{den:<3} {pct}")

    false_abstentions = [r for r in answerable if r.abstained]
    print(f"\n  false abstention rate       {len(false_abstentions)}/{len(answerable)}"
          f"  {'  '.join(r.id for r in false_abstentions)}")

    print("\n--- by category ---")
    cats: dict[str, list[Row]] = {}
    for r in rows:
        cats.setdefault(r.category, []).append(r)
    for cat, rs in cats.items():
        print(f"  {cat:<28} {sum(x.behaviour_ok for x in rs)}/{len(rs)}")

    failures = [r for r in rows if not r.behaviour_ok]
    if failures:
        print(f"\n--- {len(failures)} failure(s) ---")
        for r in failures:
            print(f"  {r.id} [{r.category}] {r.verdict}")
            print(f"      Q: {r.question}")
            print(f"      why: {r.verdict_reason[:180]}")
    else:
        print("\n  no failures")

    secs = [r.seconds for r in rows]
    print(f"\n  latency  median {statistics.median(secs):.1f}s  max {max(secs):.1f}s")

    return {k: {"passed": v[0], "total": v[1]} for k, v in metrics.items()}


def sweep(golden: list[dict], retriever: Retriever) -> None:
    """Gate threshold vs. its two error rates — retrieval only, no model calls.

    §4's tuning dial. False abstention and missed abstention trade against each
    other directly; pick the threshold off this table, not off intuition.
    """
    scores: list[tuple[dict, float]] = []
    for item in golden:
        trace = retriever.search(item["question"], expand=False)
        scores.append((item, trace.top_score))

    print(f"\n{'threshold':>10} {'false abstain':>14} {'missed abstain':>15} {'correct':>9}")
    print("-" * 52)
    for t in [x / 100 for x in range(50, 96, 5)]:
        false_ab = sum(1 for i, s in scores if not i["should_abstain"] and s < t)
        missed = sum(1 for i, s in scores if i["should_abstain"] and s >= t)
        print(f"{t:>10.2f} {false_ab:>14} {missed:>15} "
              f"{len(scores) - false_ab - missed:>9}/{len(scores)}")
    print(f"\ncurrent MIN_RERANK_SCORE = {MIN_RERANK_SCORE}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--gate", type=float, default=MIN_RERANK_SCORE)
    ap.add_argument("--category", help="run one category only")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--sweep", action="store_true", help="gate sweep, no model calls")
    ap.add_argument("--out", type=Path)
    args = ap.parse_args()

    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    if args.category:
        golden = [q for q in golden if q["category"] == args.category]
    if args.limit:
        golden = golden[: args.limit]
    if not golden:
        raise SystemExit("no questions selected")

    retriever = Retriever(verbose=False)
    _ = retriever.bm25, retriever.embeddings  # warm before threads touch them

    if args.sweep:
        sweep(golden, retriever)
        return

    qa.load_env()
    import anthropic

    client = anthropic.Anthropic(max_retries=4)

    print(f"running {len(golden)} question(s), {args.workers} workers")
    started = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        rows = list(
            pool.map(lambda q: run_one(q, retriever, client, args.gate), golden)
        )
    rows.sort(key=lambda r: r.id)
    print(f"done in {time.time() - started:.0f}s")

    metrics = report(rows, args.gate)

    RESULTS_DIR.mkdir(exist_ok=True)
    out = args.out or RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    out.write_text(
        json.dumps(
            {
                "prompt_version": prompts.QA_PROMPT_VERSION,
                "model": qa.MODEL,
                "effort": qa.EFFORT,
                "gate": args.gate,
                "metrics": metrics,
                "rows": [asdict(r) for r in rows],
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    shown = out.relative_to(ROOT) if out.is_relative_to(ROOT) else out
    print(f"\nwritten -> {shown}")


if __name__ == "__main__":
    main()
