"""Postgres session-scoped advisory lock for single-flight guards.

Used by `POST /poll` to enforce "at most one tick in flight
system-wide" across any number of replicas — the invariant used to
live in an `asyncio.Lock` and silently broke as soon as replicaCount
went above 1. See design D3 / D17.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Iterator

from psycopg import Connection
from psycopg_pool import ConnectionPool

logger = logging.getLogger(__name__)

# Stable 64-bit key for the tick lock. Chosen once and hardcoded so
# `pg_locks` inspection shows the same identifier forever regardless
# of Python hash randomization. Value derived from
# `int.from_bytes(hashlib.sha256(b"zashiki.tick").digest()[:8], "big", signed=True)`
# but pinned here to guarantee stability.
TICK_LOCK_KEY: int = -6178253175476858907


@contextlib.contextmanager
def advisory_lock(
    pool: ConnectionPool, key: int
) -> Iterator[tuple[bool, Connection]]:
    """Acquire a session-scoped Postgres advisory lock.

    Yields `(acquired, conn)`:
      - `acquired=True`: caller holds the lock; the yielded `conn` is
        held open for the entire with-block so the lock's session-scope
        covers the guarded region. Release happens automatically on
        with-exit (both explicit unlock + session-close belt-and-suspenders).
      - `acquired=False`: someone else holds it; caller should NOT do
        the guarded work — return 409 or equivalent. `conn` is still
        yielded (in case the caller wants to inspect DB state), but
        no unlock will fire.

    A hard process kill mid-lock releases via session-close TCP FIN —
    no orphan locks in normal operation. Documented recovery for the
    extreme case: `SELECT pg_advisory_unlock_all()` from any operator
    psql session frees all locks that session holds.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,))
            row = cur.fetchone()
        acquired = _extract_bool(row, "ok")
        try:
            yield acquired, conn
        finally:
            if acquired:
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT pg_advisory_unlock(%s)", (key,)
                        )
                except Exception:
                    # Unlock failure is not actionable — the lock will
                    # release when the session closes anyway. Log so a
                    # pattern of failures is visible; don't raise.
                    logger.warning(
                        "advisory_lock: pg_advisory_unlock raised; "
                        "relying on session-close release"
                    )


def _extract_bool(row: object, key: str) -> bool:
    """Coerce a psycopg row to a bool. Works with both dict_row and
    tuple_row cursors so callers don't need to care about the pool's
    row factory."""
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get(key))
    if isinstance(row, (list, tuple)) and row:
        return bool(row[0])
    return bool(row)
