"""Label-review CLI (issue #6): human-verify LLM classifications.

Reads `data/training_pairs.jsonl` (from backfill_training_pairs.py) and
walks the operator through confirming or correcting each LLM label.
Verdicts append to `data/verified_labels.jsonl`; re-runs skip already-
verified message_ids, so a 274-row audit can span several sessions.

Primary mission today: the 274 pre-v1.4.1 `講座資訊` suspects — the
old classifier boundary let Coursera-style promos into that class, so
none of those rows can sit in the fine-tune TEST set until a human
looks at them. `--suspect-only` targets exactly that population.

    uv run python scripts/label_review.py --suspect-only
    uv run python scripts/label_review.py --category 廣告 --sample 50
    uv run python scripts/label_review.py --sample 100          # random spot-check

Keys at the prompt:
    [Enter]  LLM label is correct
    1-9/0/a-e  pick the correct category (menu shows the mapping)
    s        skip (no verdict recorded; reappears next run)
    n        add a free-text note, then decide
    q        save and quit

Output rows (data/verified_labels.jsonl):
    {"message_id", "llm_original", "verified", "corrected", "verified_at", "note"?}

When the laya shadow table exists (Phase 0 deployment), this tool grows
the disagreement-only mode from issue #6; today's JSONL source carries
no laya columns, so those flags are absent.

PRIVACY: input and output live in gitignored data/ — real mail text.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
from datetime import datetime, timezone

# Category order defines the hotkey map: 1-9, 0, a-e.
CATEGORIES = [
    "消費支出", "消費資訊彙整", "點數資訊彙整", "訂閱服務", "技術文章",
    "講座資訊", "會議邀請", "帳單通知", "廣告", "促銷",
    "社交", "新聞", "安全通知", "股票資訊", "其他",
]
HOTKEYS = ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "a", "b", "c", "d", "e"]
KEY_TO_CATEGORY = dict(zip(HOTKEYS, CATEGORIES))

V141_CUTOFF = datetime(2026, 9, 23, tzinfo=timezone.utc)

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def _is_suspect(row: dict) -> bool:
    if row["category"] != "講座資訊":
        return False
    analyzed = datetime.fromisoformat(row["analyzed_at"])
    if analyzed.tzinfo is None:
        analyzed = analyzed.replace(tzinfo=timezone.utc)
    return analyzed < V141_CUTOFF


def _load_verified(path: str) -> set[str]:
    done: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    done.add(json.loads(line)["message_id"])
    except FileNotFoundError:
        pass
    return done


def _menu() -> str:
    cells = [
        f"{CYAN}{k}{RESET}) {c}" for k, c in zip(HOTKEYS, CATEGORIES)
    ]
    lines = []
    for i in range(0, 15, 3):
        lines.append("  " + "  ".join(f"{cell:<18s}" for cell in cells[i:i + 3]))
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", default="data/training_pairs.jsonl")
    ap.add_argument("--output", default="data/verified_labels.jsonl")
    ap.add_argument("--category", default=None,
                    help="only rows the LLM labeled with this category")
    ap.add_argument("--suspect-only", action="store_true",
                    help="only pre-v1.4.1 講座資訊 rows")
    ap.add_argument("--sample", type=int, default=None,
                    help="random sample size from the filtered pool")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--body-chars", type=int, default=600,
                    help="input_text preview length")
    args = ap.parse_args()

    done = _load_verified(args.output)
    pool: list[dict] = []
    for line in open(args.input, encoding="utf-8"):
        row = json.loads(line)
        if row.get("deleted") or row["message_id"] in done:
            continue
        if args.category and row["category"] != args.category:
            continue
        if args.suspect_only and not _is_suspect(row):
            continue
        pool.append(row)

    rng = random.Random(args.seed)
    rng.shuffle(pool)
    if args.sample is not None:
        pool = pool[: args.sample]
    if not pool:
        print("nothing to review (all done, or filters matched nothing)")
        return 0

    print(f"{len(pool)} rows to review "
          f"({len(done)} already verified in {args.output})\n")

    confirmed = corrected = skipped = 0
    out = open(args.output, "a", encoding="utf-8")
    try:
        for i, row in enumerate(pool, 1):
            suspect_tag = f" {YELLOW}[pre-v1.4.1 suspect]{RESET}" if _is_suspect(row) else ""
            print(f"{DIM}{'─' * 62}{RESET}")
            print(f"{BOLD}#{i}/{len(pool)}{RESET}  {row['message_id']}"
                  f"  {DIM}{row['analyzed_at'][:16]}{RESET}{suspect_tag}")
            print()
            preview = row["input_text"][: args.body_chars]
            print("    " + preview.replace("\n", "\n    "))
            if len(row["input_text"]) > args.body_chars:
                print(f"    {DIM}… ({len(row['input_text'])} chars total){RESET}")
            print()
            print(f"LLM label: {BOLD}{GREEN}[{row['category']}]{RESET}")
            print(_menu())

            note = None
            while True:
                try:
                    key = input(
                        f"[Enter]=正確 [1-9/0/a-e]=更正 [s]kip [n]ote [q]uit > "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    key = "q"
                if key == "n":
                    note = input("note > ").strip() or None
                    continue
                break

            if key == "q":
                print("saved; bye")
                break
            if key == "s":
                skipped += 1
                continue
            if key == "" or key == row["category"]:
                verified = row["category"]
            elif key in KEY_TO_CATEGORY:
                verified = KEY_TO_CATEGORY[key]
            else:
                print(f"{YELLOW}unrecognized key {key!r}; skipping row{RESET}")
                skipped += 1
                continue

            record = {
                "message_id": row["message_id"],
                "llm_original": row["category"],
                "verified": verified,
                "corrected": verified != row["category"],
                "verified_at": datetime.now(tz=timezone.utc).isoformat(),
            }
            if note:
                record["note"] = note
            out.write(json.dumps(record, ensure_ascii=False) + "\n")
            out.flush()
            if record["corrected"]:
                corrected += 1
                print(f"{YELLOW}→ corrected to [{verified}]{RESET}")
            else:
                confirmed += 1
    finally:
        out.close()

    total = confirmed + corrected
    rate = f"{corrected / total:.1%}" if total else "n/a"
    print(f"\nsession: confirmed={confirmed} corrected={corrected} "
          f"skipped={skipped} correction-rate={rate}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
