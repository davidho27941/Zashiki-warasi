"""Seed v1 classification history for analyses that have none.

Same idempotent statement as alembic 0009. Run once after each deploy
of the taxonomy-v2 release (and after the v2 cutover) so analyses the
old pod wrote between the migration and its shutdown get their v1
history row. Re-running is harmless.

Runs inside the zashiki pod (in-cluster DATABASE_URL):

    POD=$(kubectl -n zashiki get pod -l app.kubernetes.io/name=zashiki-warasi \\
          -o jsonpath='{.items[0].metadata.name}')
    kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \\
        python - < scripts/seed_classifications.py

Prints the inserted count and whether every analysis now has history.
"""

from __future__ import annotations

import sys

from sqlalchemy import create_engine, text

from zashiki_warasi.classifier.classification_store import SEED_V1_HISTORY_SQL
from zashiki_warasi.core.config import DatabaseSettings


def main() -> int:
    engine = create_engine(DatabaseSettings().database_url)
    with engine.begin() as conn:
        inserted = conn.execute(text(SEED_V1_HISTORY_SQL)).rowcount
        missing = conn.execute(text(
            "SELECT count(*) FROM email_analyses a WHERE NOT EXISTS ("
            "SELECT 1 FROM email_classifications c "
            "WHERE c.message_id = a.message_id)"
        )).scalar_one()
        current = conn.execute(text(
            "SELECT count(*) FROM email_category_current"
        )).scalar_one()
        analyses = conn.execute(text(
            "SELECT count(*) FROM email_analyses"
        )).scalar_one()
    print(f"inserted={inserted} analyses={analyses} "
          f"current={current} missing={missing}")
    return 0 if missing == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
