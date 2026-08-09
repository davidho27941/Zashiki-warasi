"""Application-scoped service container.

Bundles every long-lived collaborator the FastAPI handlers and CLI
subcommands need. Constructed once at process startup (FastAPI
lifespan, CLI entry) via `build_services()`; torn down via
`close_services()`. Handlers pull from it via
`zashiki_warasi.web.dependencies.get_services`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from google.oauth2.credentials import Credentials
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.coordination.oauth_flow_store import (
    OAuthFlowStore,
    ensure_oauth_flows_table,
)
from zashiki_warasi.core.config import (
    DatabaseSettings,
    GmailSettings,
    HttpSettings,
    NotionSettings,
    OAuthSettings,
)
from zashiki_warasi.core.db import get_session_factory
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.poller import Poller
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)

# Matches the value used by the legacy `app.py` bootstrap so the
# `pg_stat_activity` label stays stable across v0.6 → v1.0 upgrades.
_CHECKPOINTER_APPLICATION_NAME = "zashiki-warasi-checkpointer"


@dataclass
class Services:
    """Container of long-lived collaborators.

    Frozen post-construction — handlers should never mutate fields.
    New collaborators added by later groups (e.g. `oauth_flow_store`
    in Group 4) land here as new fields.
    """

    http_settings: HttpSettings
    oauth_settings: OAuthSettings
    gmail_settings: GmailSettings
    checkpointer_pool: ConnectionPool
    checkpointer: PostgresSaver
    session_factory: sessionmaker
    credentials: Credentials
    gmail_client: GmailClient
    agent: EmailAgent
    poller: Poller
    notifier: TelegramNotifier
    notion: NotionExpenseRecorder | None
    oauth_flow_store: OAuthFlowStore
    stop_event: threading.Event


def _libpq_url(sqlalchemy_url: str) -> str:
    """Strip SQLAlchemy's `+psycopg` driver suffix for libpq consumers."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_pool(
    db_settings: DatabaseSettings,
    stop_event: threading.Event,
    unreachable_flag: threading.Event,
) -> ConnectionPool:
    """psycopg-pool ConnectionPool wired for LangGraph + our logging.

    Mirrors the pool built by `zashiki_warasi.app._build_checkpointer_pool`
    (still used by the legacy CLI path). Kept as a private helper here
    so `build_services()` can be called from both CLI + FastAPI lifespan
    without importing from `app.py` (which would circular-import via
    the CLI's click group).
    """
    import psycopg  # noqa: F401 — used by the closures' type only

    def _configure(conn) -> None:
        conn.execute(
            "SELECT set_config('application_name', %s, false)",
            (_CHECKPOINTER_APPLICATION_NAME,),
        )
        logger.debug(
            f"checkpointer conn created "
            f"(application_name={_CHECKPOINTER_APPLICATION_NAME})"
        )

    def _check(conn) -> None:
        try:
            ConnectionPool.check_connection(conn)
        except Exception:
            logger.warning(
                "checkpointer pool: discarded stale connection, reconnecting"
            )
            raise

    def _reconnect_failed(pool: ConnectionPool) -> None:
        logger.critical(
            f"checkpointer pool: reconnect failed after "
            f"{pool.reconnect_timeout}s — DB unreachable"
        )
        unreachable_flag.set()
        stop_event.set()

    return ConnectionPool(
        _libpq_url(db_settings.database_url),
        min_size=db_settings.checkpointer_pool_min_size,
        max_size=db_settings.checkpointer_pool_max_size,
        max_lifetime=db_settings.checkpointer_pool_max_lifetime_seconds,
        max_idle=db_settings.checkpointer_pool_max_idle_seconds,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        check=_check,
        configure=_configure,
        reconnect_failed=_reconnect_failed,
        open=False,
    )


def _build_notion(notion_settings: NotionSettings) -> NotionExpenseRecorder | None:
    if notion_settings.token and notion_settings.expense_database_id:
        return NotionExpenseRecorder(notion_settings)
    logger.info(
        "Notion integration disabled (NOTION_TOKEN or "
        "NOTION_EXPENSE_DATABASE_ID not set)"
    )
    return None


def build_services(
    *,
    stop_event: threading.Event | None = None,
    unreachable_flag: threading.Event | None = None,
) -> Services:
    """Construct every collaborator, open the DB pool, run
    `PostgresSaver.setup()`, and return a Services container.

    The pool is opened here (not lazily) so a broken `DATABASE_URL`
    surfaces at process start rather than on the first `/poll` request.
    Caller is expected to invoke `close_services()` on shutdown so
    background threads (pool's) exit cleanly.
    """
    stop_event = stop_event or threading.Event()
    unreachable_flag = unreachable_flag or threading.Event()

    http_settings = HttpSettings()
    oauth_settings = OAuthSettings()
    gmail_settings = GmailSettings()
    db_settings = DatabaseSettings()
    notion_settings = NotionSettings()

    session_factory = get_session_factory()
    notifier = TelegramNotifier()

    # Credentials failure at bootstrap is fatal — surface via exception
    # so the FastAPI lifespan aborts startup and CLI can `sys.exit(78)`.
    credentials = get_credentials(gmail_settings)
    gmail_client = GmailClient(
        credentials,
        http_timeout_seconds=gmail_settings.http_timeout_seconds,
    )

    pool = _build_pool(db_settings, stop_event, unreachable_flag)
    pool.open(wait=True)
    logger.info(
        f"checkpointer pool opened "
        f"(min={db_settings.checkpointer_pool_min_size} "
        f"max={db_settings.checkpointer_pool_max_size} "
        f"max_lifetime={db_settings.checkpointer_pool_max_lifetime_seconds}s "
        f"max_idle={db_settings.checkpointer_pool_max_idle_seconds}s)"
    )
    checkpointer = PostgresSaver(pool)
    checkpointer.setup()
    # Group 4: coordination schema. Idempotent — runs every startup.
    ensure_oauth_flows_table(pool)
    oauth_flow_store = OAuthFlowStore(
        pool,
        client_secrets_path=gmail_settings.credentials_path,
    )

    notion = _build_notion(notion_settings)
    agent = EmailAgent(
        checkpointer=checkpointer,
        session_factory=session_factory,
        notifier=notifier,
        client=gmail_client,
        notion=notion,
    )
    poller = Poller(
        client=gmail_client,
        session_factory=session_factory,
        handler=agent.handle_email,
        stop_event=stop_event,
        notifier=notifier,
    )

    return Services(
        http_settings=http_settings,
        oauth_settings=oauth_settings,
        gmail_settings=gmail_settings,
        checkpointer_pool=pool,
        checkpointer=checkpointer,
        session_factory=session_factory,
        credentials=credentials,
        gmail_client=gmail_client,
        agent=agent,
        poller=poller,
        notifier=notifier,
        notion=notion,
        oauth_flow_store=oauth_flow_store,
        stop_event=stop_event,
    )


def close_services(services: Services) -> None:
    """Close the DB pool + set stop_event so background threads exit."""
    services.stop_event.set()
    try:
        services.checkpointer_pool.close()
        logger.info("checkpointer pool closed")
    except Exception:
        logger.exception("Failed to close checkpointer pool cleanly")
