"""SQLAlchemy engine and session factory."""

from __future__ import annotations

import logging
from functools import lru_cache

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from zashiki_warasi.core.config import DatabaseSettings

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = DatabaseSettings()
    return create_engine(settings.database_url, future=True)


@lru_cache(maxsize=1)
def get_session_factory() -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)


# Tables wiped by `reset_database`. Two groups:
#   * Application domain — created by Alembic migrations, always present
#     after `alembic upgrade head`.
#   * LangGraph PostgresSaver — created at runtime by `checkpointer.setup()`
#     on first poller boot, so they may NOT exist yet on a fresh install.
# We look up the intersection with `information_schema.tables` before
# issuing the TRUNCATE so a not-yet-set-up checkpoint table doesn't crash
# the reset.
_RESET_TARGET_TABLES = (
    "gmail_sync_state",
    "notion_sync_state",
    "processed_messages",
    "email_analyses",
    "expenses",
    "checkpoints",
    "checkpoint_writes",
    "checkpoint_blobs",
    "checkpoint_migrations",
)


def reset_database() -> None:
    """TRUNCATE every app + LangGraph checkpoint table.

    Used by the `zashiki-warasi --reset` CLI flag to start the poller
    from a clean slate. Caller is responsible for any confirmation —
    this function unconditionally truncates.

    Postgres-only: uses `TRUNCATE ... CASCADE`, which has no SQLite
    equivalent. The LangGraph checkpoint tables in particular are
    Postgres-specific (`langgraph-checkpoint-postgres`).
    """
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' "
                "AND table_name = ANY(:names)"
            ),
            {"names": list(_RESET_TARGET_TABLES)},
        )
        existing = sorted(row[0] for row in result)

        if not existing:
            logger.info(
                "No target tables found — database appears empty; "
                "skipping reset."
            )
            return

        # Table names are hard-coded constants, not user input, so
        # f-string interpolation here is not an injection risk.
        # Quoting is defensive in case any name ever needs it.
        quoted = ", ".join(f'"{name}"' for name in existing)
        conn.execute(
            text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE")
        )
        logger.info(f"Truncated {len(existing)} tables: {existing}")
