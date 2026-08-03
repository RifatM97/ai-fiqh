"""Write reviewed `expected_chunk_ids` into the golden set.

Separate from `label_chunks.py` so that the proposing step (which is a heuristic)
and the committing step (which is a human decision) are never the same run.
Only adds fields -- every hand-written key in the golden set is preserved.

Run: uv run python eval/apply_labels.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "eval" / "golden-eval-set.json"
PROPOSALS_PATH = ROOT / "eval" / "label-proposals.json"

# Reviewed 2026-08-03. The answer-coverage heuristic picked
# `041-the-sunan-of-salah-p0` because the reference answer's words (standing,
# rukū‘, sujūd) occur in the sunan list too. The actual definition -- "Arkān of
# ṣalāh / Four from the above-mentioned twenty seven are arkān" -- is on p37,
# where retrieval had it ranked first all along.
MANUAL_OVERRIDES: dict[str, list[str]] = {
    "Q05": ["036-the-prerequisites-of-salah-and-its-components-p1"],
}

# Transliteration questions are the same question asked three ways; the metric is
# that every member retrieves identically, which needs the grouping made explicit.
VARIANT_GROUPS: dict[str, str] = {
    "Q17": "wudu-fard", "Q18": "wudu-fard", "Q19": "wudu-fard",
    "Q20": "sawm-kaffarah", "Q21": "sawm-kaffarah", "Q22": "sawm-kaffarah",
    "Q23": "zakah-obligation", "Q24": "zakah-obligation",
}


def main() -> None:
    golden = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))
    proposals = {p["id"]: p for p in json.loads(PROPOSALS_PATH.read_text(encoding="utf-8"))}

    backup = GOLDEN_PATH.with_suffix(".json.bak")
    if not backup.exists():
        shutil.copy2(GOLDEN_PATH, backup)
        print(f"backup written -> {backup.name}")

    before_keys = {q["id"]: set(q) for q in golden}
    for q in golden:
        p = proposals[q["id"]]
        q["expected_chunk_ids"] = MANUAL_OVERRIDES.get(q["id"], p["proposed_chunk_ids"])
        q["should_abstain"] = p["abstain_expected"]
        if q["id"] in VARIANT_GROUPS:
            q["variant_group"] = VARIANT_GROUPS[q["id"]]

    lost = {qid: before_keys[qid] - set(q) for q in golden for qid in [q["id"]] if before_keys[qid] - set(q)}
    if lost:
        raise SystemExit(f"refusing to write, fields would be lost: {lost}")

    GOLDEN_PATH.write_text(
        json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    labelled = sum(1 for q in golden if q["expected_chunk_ids"])
    abstain = sum(1 for q in golden if q["should_abstain"])
    print(f"golden set updated -> {GOLDEN_PATH.name}")
    print(f"  {len(golden)} questions, {labelled} with a chunk label, {abstain} abstention-only")
    print(f"  {len(MANUAL_OVERRIDES)} manual override(s): {list(MANUAL_OVERRIDES)}")
    print(f"  {len(set(VARIANT_GROUPS.values()))} transliteration variant groups")


if __name__ == "__main__":
    main()
