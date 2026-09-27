"""Task 0.3: split + balance the backfilled pairs for laya RLCD fine-tune.

Reads `data/training_pairs.jsonl` (from backfill_training_pairs.py) and
writes:

    data/train.jsonl   — class-balanced training set (majority capped)
    data/test.jsonl    — stratified held-out at NATURAL distribution

Design choices (openspec add-laya-shadow-classifier, task 0.3):

- Split FIRST (stratified, fixed seed), balance the TRAIN side only.
  The test set keeps the natural class distribution so held-out
  accuracy approximates production accuracy; the train side caps
  majority classes (促銷 1.7k → cap) so the model can't win by
  majority-guessing.
- 講座資訊 rows analyzed BEFORE v1.4.1 (2026-09-23) are `suspect`:
  the classifier boundary was loose then (Coursera promos landed in
  講座資訊). Suspect rows stay in TRAIN (noise the balance step
  tolerates) but are EXCLUDED from TEST — the eval gate must judge
  against clean labels. They're flagged in train output for the
  issue-#6 audit to revisit.
- Deterministic: seed fixed, rows sorted before shuffling, so the
  split is reproducible from the same input file.

PRIVACY: inputs and outputs contain real mail text; data/ is
gitignored and must stay off third-party services and the public repo.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from collections import defaultdict
from datetime import datetime, timezone

V141_CUTOFF = datetime(2026, 9, 23, tzinfo=timezone.utc)
SUSPECT_CATEGORY = "講座資訊"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="data/training_pairs.jsonl")
    ap.add_argument("--train-out", default="data/train.jsonl")
    ap.add_argument("--test-out", default="data/test.jsonl")
    ap.add_argument("--test-frac", type=float, default=0.15)
    ap.add_argument("--train-cap", type=int, default=500,
                    help="max TRAIN rows per class after split")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--verified", default="data/verified_labels.jsonl",
                    help="label_review.py output; corrections are applied "
                         "and human-confirmed rows lose their suspect flag")
    args = ap.parse_args()

    # Human verdicts (issue #6 CLI). A verified row's label REPLACES the
    # LLM's; a confirmed-or-corrected row is no longer suspect — it has
    # been looked at, so it becomes eligible for the TEST split.
    verified: dict[str, str] = {}
    try:
        for line in open(args.verified, encoding="utf-8"):
            if line.strip():
                v = json.loads(line)
                verified[v["message_id"]] = v["verified"]
    except FileNotFoundError:
        pass
    if verified:
        print(f"applying {len(verified)} human verdicts from {args.verified}",
              file=sys.stderr)

    corrections_applied = 0
    by_class: dict[str, list[dict]] = defaultdict(list)
    for line in open(args.input, encoding="utf-8"):
        row = json.loads(line)
        if row.get("deleted"):
            continue
        analyzed = datetime.fromisoformat(row["analyzed_at"])
        if analyzed.tzinfo is None:
            analyzed = analyzed.replace(tzinfo=timezone.utc)
        human = verified.get(row["message_id"])
        if human is not None:
            if human != row["category"]:
                corrections_applied += 1
            row["category"] = human
            row["suspect"] = False  # human-reviewed
        else:
            row["suspect"] = (
                row["category"] == SUSPECT_CATEGORY and analyzed < V141_CUTOFF
            )
        by_class[row["category"]].append(row)
    if corrections_applied:
        print(f"{corrections_applied} labels corrected by human review",
              file=sys.stderr)

    rng = random.Random(args.seed)
    train: list[dict] = []
    test: list[dict] = []
    stats = {}
    for category in sorted(by_class):
        rows = sorted(by_class[category], key=lambda r: r["message_id"])
        rng.shuffle(rows)
        clean = [r for r in rows if not r["suspect"]]
        suspect = [r for r in rows if r["suspect"]]

        n_test = max(1, round(len(clean) * args.test_frac)) if clean else 0
        cls_test = clean[:n_test]
        cls_train_pool = clean[n_test:] + suspect  # suspects: train only
        rng.shuffle(cls_train_pool)
        cls_train = cls_train_pool[: args.train_cap]

        train.extend(cls_train)
        test.extend(cls_test)
        stats[category] = {
            "total": len(rows),
            "suspect": len(suspect),
            "train": len(cls_train),
            "train_dropped_by_cap": max(0, len(cls_train_pool) - args.train_cap),
            "test": len(cls_test),
        }

    rng.shuffle(train)
    rng.shuffle(test)
    with open(args.train_out, "w", encoding="utf-8") as fh:
        for r in train:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")
    with open(args.test_out, "w", encoding="utf-8") as fh:
        for r in test:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(json.dumps({
        "train_total": len(train),
        "test_total": len(test),
        "per_class": stats,
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
