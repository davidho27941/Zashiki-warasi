"""Backfill laya fine-tune training pairs from historical analyses.

The `email_analyses` table stores labels but no email text, so this
script re-fetches each labeled message from Gmail and pairs it with the
label, truncated through the SAME `build_classifier_input` function the
shadow client / future engine will use — train/serve consistency by
construction (openspec add-laya-shadow-classifier, D10 / task 0.1).

Designed to run INSIDE the zashiki pod so the PVC's OAuth token and the
in-cluster DATABASE_URL are reused (no new grants, no port-forwards):

    POD=$(kubectl -n zashiki get pod -l app.kubernetes.io/name=zashiki-warasi \
          -o jsonpath='{.items[0].metadata.name}')
    kubectl -n zashiki exec -i "$POD" -- python - \
        --output /tmp/training_pairs.jsonl < scripts/backfill_training_pairs.py
    kubectl -n zashiki cp "$POD":/tmp/training_pairs.jsonl data/training_pairs.jsonl

Also runs on a laptop with a local `.env` (readonly Gmail token +
reachable DATABASE_URL) — same flags.

Output: JSONL, one of
    {"message_id","category","analyzed_at","input_text"}   — a usable pair
    {"message_id","deleted":true}                          — Gmail 404 tombstone
Resumable: existing output rows (pairs AND tombstones) are skipped on
re-run. Transient fetch errors are NOT tombstoned, so a re-run retries
them. PRIVACY: the output contains real mail text — it stays on local
machines / nttu-gpu-lab, never a third-party service, never the repo
(`data/` is gitignored).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime

from sqlalchemy import select

from zashiki_warasi.classifier import build_classifier_input
from zashiki_warasi.core.db import get_session_factory
from zashiki_warasi.core.models import EmailAnalysis
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient, MessageNotFoundError
from zashiki_warasi.core.config import GmailSettings

# Abort if Gmail errors this many times in a row — a stuck token or a
# quota lockout should stop the run loudly, not burn the whole table.
MAX_CONSECUTIVE_ERRORS = 20


def _load_done(path: str) -> set[str]:
    done: set[str] = set()
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    done.add(json.loads(line)["message_id"])
                except (ValueError, KeyError):
                    print(f"skipping malformed resume line: {line[:80]}",
                          file=sys.stderr)
    except FileNotFoundError:
        pass
    return done


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--output", default="training_pairs.jsonl")
    ap.add_argument("--limit", type=int, default=None,
                    help="cap processed rows (smoke runs)")
    ap.add_argument("--sleep", type=float, default=0.05,
                    help="pause between Gmail fetches, seconds")
    ap.add_argument("--since", default=None,
                    help="only rows with analyzed_at >= this ISO date")
    args = ap.parse_args()

    done = _load_done(args.output)
    if done:
        print(f"resume: {len(done)} message_ids already in {args.output}",
              file=sys.stderr)

    session_factory = get_session_factory()
    settings = GmailSettings()
    client = GmailClient(
        get_credentials(settings),
        http_timeout_seconds=settings.http_timeout_seconds,
        settings=settings,
    )

    stmt = select(
        EmailAnalysis.message_id,
        EmailAnalysis.category,
        EmailAnalysis.analyzed_at,
    ).order_by(EmailAnalysis.analyzed_at)
    if args.since:
        stmt = stmt.where(
            EmailAnalysis.analyzed_at >= datetime.fromisoformat(args.since)
        )
    with session_factory() as session:
        rows = session.execute(stmt).all()
    print(f"{len(rows)} labeled rows in email_analyses", file=sys.stderr)

    fetched = deleted = errors = skipped = 0
    consecutive_errors = 0
    per_category: dict[str, int] = {}
    started = time.monotonic()

    with open(args.output, "a", encoding="utf-8") as out:
        for i, (message_id, category, analyzed_at) in enumerate(rows):
            if args.limit is not None and fetched + deleted >= args.limit:
                break
            if message_id in done:
                skipped += 1
                continue
            try:
                email = client.get_message(message_id)
            except MessageNotFoundError:
                out.write(json.dumps(
                    {"message_id": message_id, "deleted": True}
                ) + "\n")
                deleted += 1
                consecutive_errors = 0
            except Exception as exc:  # noqa: BLE001 — transient; retry next run
                errors += 1
                consecutive_errors += 1
                print(f"fetch error {message_id}: "
                      f"{type(exc).__name__}: {str(exc)[:120]}",
                      file=sys.stderr)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    print(f"aborting: {consecutive_errors} consecutive errors",
                          file=sys.stderr)
                    break
            else:
                out.write(json.dumps({
                    "message_id": message_id,
                    "category": category,
                    "analyzed_at": analyzed_at.isoformat(),
                    "input_text": build_classifier_input(email),
                }, ensure_ascii=False) + "\n")
                fetched += 1
                consecutive_errors = 0
                per_category[category] = per_category.get(category, 0) + 1

            if (fetched + deleted + errors) % 20 == 0:
                out.flush()
            if (fetched + deleted + errors) % 100 == 0 and (fetched + deleted + errors):
                rate = (fetched + deleted + errors) / (time.monotonic() - started)
                print(f"progress {i + 1}/{len(rows)} "
                      f"fetched={fetched} deleted={deleted} errors={errors} "
                      f"({rate:.1f}/s)", file=sys.stderr)
            if args.sleep:
                time.sleep(args.sleep)

    print(json.dumps({
        "fetched": fetched,
        "deleted_tombstones": deleted,
        "transient_errors": errors,
        "resume_skipped": skipped,
        "elapsed_s": round(time.monotonic() - started, 1),
        "per_category": dict(sorted(per_category.items(),
                                    key=lambda kv: -kv[1])),
    }, ensure_ascii=False, indent=2), file=sys.stderr)
    return 0 if errors < MAX_CONSECUTIVE_ERRORS else 1


if __name__ == "__main__":
    sys.exit(main())
