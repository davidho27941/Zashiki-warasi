"""Cutover gate: list LangGraph threads holding an unpersisted analysis.

A thread whose latest checkpoint carries an `analysis` but whose
message has no `email_analyses` row was interrupted after analyze and
will resume on the next handle of that message. Before flipping
`TAXONOMY_VERSION`, this list must be empty — re-handle those messages
under the current version first (cutover runbook). Threads with no
analysis (failed or not yet analyzed) are not listed: on resume they
analyze under whatever version is active, which is safe.

Runs inside the zashiki pod:

    kubectl -n zashiki exec -i "$POD" -c zashiki-warasi -- \\
        python - < scripts/check_pending_threads.py

Exit 0 = clear to cut over; exit 1 = pending threads listed.
"""

from __future__ import annotations

import sys

from langgraph.checkpoint.postgres import PostgresSaver
from sqlalchemy import create_engine, text

from zashiki_warasi.core.config import DatabaseSettings
from zashiki_warasi.core.services import _libpq_url

CANDIDATES_SQL = """
SELECT DISTINCT cp.thread_id
FROM checkpoints cp
WHERE cp.checkpoint_ns = ''
  AND NOT EXISTS (
      SELECT 1 FROM email_analyses a WHERE a.message_id = cp.thread_id
  )
ORDER BY cp.thread_id
"""


def find_pending(saver, thread_ids) -> list[tuple[str, str, str]]:
    """(thread_id, category, taxonomy_version) per thread whose latest
    checkpoint holds an analysis."""
    pending = []
    for thread_id in thread_ids:
        snapshot = saver.get_tuple(
            {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        )
        if snapshot is None:
            continue
        analysis = snapshot.checkpoint.get("channel_values", {}).get("analysis")
        if analysis is None:
            continue
        pending.append((
            thread_id,
            analysis.category,
            getattr(analysis, "taxonomy_version", "v1"),
        ))
    return pending


def main() -> int:
    url = DatabaseSettings().database_url
    with create_engine(url).connect() as conn:
        candidates = conn.execute(text(CANDIDATES_SQL)).scalars().all()
    with PostgresSaver.from_conn_string(_libpq_url(url)) as saver:
        pending = find_pending(saver, candidates)
    for thread_id, category, version in pending:
        print(f"pending  {thread_id}  {version}  {category}")
    print(f"threads_without_analysis_row={len(candidates)} "
          f"pending_with_analysis={len(pending)}")
    return 1 if pending else 0


if __name__ == "__main__":
    sys.exit(main())
