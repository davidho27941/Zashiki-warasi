"""Import blind human verdicts into the classification history.

Reads label-review JSONL (`label_review.py --blind` / `label_review_web.py`
output), keeps the LAST record per message_id, and records each verdict
through the SCD-2 writer as `source='human'`, `recorded_by='import'`,
`valid_from=verified_at`. Human rows take precedence: later LLM writes
for the same message are no-ops.

The taxonomy version is declared for the whole file, because the JSONL
does not carry it: the Phase 2 file is v1, relabels made after the v2
cutover are v2.

Rejected (reported, not imported):
- records without `"blind": true` (anchored verdicts are not ground truth);
- a `verified` category outside the declared version;
- message_ids with no `email_analyses` row.

    # copy the file into the pod, then run there (in-cluster DATABASE_URL)
    kubectl -n zashiki cp data/relabel/phase2_verified.jsonl \\
        "$POD":/tmp/verdicts.jsonl -c zashiki-warasi
    kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- python - \\
        --input /tmp/verdicts.jsonl --taxonomy-version v1 \\
        < scripts/import_human_labels.py

`--dry-run` evaluates everything and rolls back.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from zashiki_warasi.classifier import classification_store
from zashiki_warasi.core.config import DatabaseSettings
from zashiki_warasi.core.models import EmailAnalysis
from zashiki_warasi.core.schemas import CATEGORIES_BY_VERSION


def load_verdicts(lines) -> dict[str, dict]:
    """Last record per message_id wins (undo appends a newer record)."""
    verdicts: dict[str, dict] = {}
    for line in lines:
        if line.strip():
            record = json.loads(line)
            verdicts[record["message_id"]] = record
    return verdicts


def _verified_at(record: dict) -> datetime:
    at = datetime.fromisoformat(record["verified_at"])
    return at if at.tzinfo else at.replace(tzinfo=timezone.utc)


def import_verdicts(
    session: Session, verdicts: dict[str, dict], taxonomy_version: str,
) -> tuple[Counter, list[str]]:
    """Record verdicts in chronological order. Returns (outcome counts,
    rejection lines). Does not commit."""
    vocabulary = CATEGORIES_BY_VERSION[taxonomy_version]
    counts: Counter = Counter()
    rejected: list[str] = []
    for mid, record in sorted(
        verdicts.items(), key=lambda kv: _verified_at(kv[1]),
    ):
        category = record.get("verified")
        if record.get("blind") is not True:
            rejected.append(f"{mid}: not a blind verdict")
        elif category not in vocabulary:
            rejected.append(
                f"{mid}: {category!r} not in taxonomy {taxonomy_version}"
            )
        elif session.get(EmailAnalysis, mid) is None:
            rejected.append(f"{mid}: no email_analyses row")
        else:
            try:
                counts[classification_store.record(
                    session,
                    message_id=mid,
                    category=category,
                    taxonomy_version=taxonomy_version,
                    source="human",
                    recorded_by="import",
                    at=_verified_at(record),
                )] += 1
            except ValueError as exc:
                rejected.append(f"{mid}: {exc}")
    counts["rejected"] = len(rejected)
    return counts, rejected


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True)
    ap.add_argument("--taxonomy-version", required=True,
                    choices=sorted(CATEGORIES_BY_VERSION))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with open(args.input, encoding="utf-8") as fh:
        verdicts = load_verdicts(fh)
    engine = create_engine(DatabaseSettings().database_url)
    with Session(engine) as session:
        counts, rejected = import_verdicts(
            session, verdicts, args.taxonomy_version,
        )
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    for line in rejected:
        print(f"rejected  {line}")
    print(("DRY RUN " if args.dry_run else "")
          + " ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
