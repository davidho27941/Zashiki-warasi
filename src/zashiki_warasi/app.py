"""Application entry point.

Wires Gmail credentials -> GmailClient -> EmailAgent (with LangGraph
PostgresSaver checkpointer) -> Poller, then blocks on the polling loop.
SIGINT / SIGTERM are caught and turned into a graceful shutdown so the
message currently being processed always gets its `processed_messages`
row written before exit.
"""

from __future__ import annotations

import logging
import signal
import threading

import click
from langgraph.checkpoint.postgres import PostgresSaver

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.core.config import DatabaseSettings, NotionSettings
from zashiki_warasi.core.db import get_session_factory, reset_database
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.poller import Poller
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


def _init_logging() -> None:
    """Configure root logging once. Idempotent — repeat calls no-op."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _libpq_url(sqlalchemy_url: str) -> str:
    """Strip SQLAlchemy's `+psycopg` driver suffix for libpq consumers."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _build_notion() -> NotionExpenseRecorder | None:
    """Return a NotionExpenseRecorder if both env vars are set, else None.

    The whole Notion integration is feature-flagged by configuration —
    missing token / database id means we don't even import-time
    instantiate the client, so users without Notion accounts have no
    extra dependency to think about at runtime.
    """
    settings = NotionSettings()
    if settings.token and settings.expense_database_id:
        return NotionExpenseRecorder(settings)
    logger.info(
        "Notion integration disabled (NOTION_TOKEN or "
        "NOTION_EXPENSE_DATABASE_ID not set)"
    )
    return None


def _install_shutdown_handlers(stop_event: threading.Event) -> None:
    """SIGINT/SIGTERM -> set `stop_event`; second SIGINT forces exit."""
    state = {"sigint_count": 0}

    def handler(signum: int, _frame) -> None:
        name = signal.Signals(signum).name
        if signum == signal.SIGINT:
            state["sigint_count"] += 1
            if state["sigint_count"] >= 2:
                logger.warning(
                    f"{name} received again; restoring default handler "
                    "and forcing exit"
                )
                signal.signal(signal.SIGINT, signal.default_int_handler)
                raise KeyboardInterrupt
            logger.warning(
                f"{name} received; finishing current message, then "
                "exiting. Press Ctrl+C again to force quit."
            )
        else:
            logger.warning(
                f"{name} received; finishing current message, then exiting"
            )
        stop_event.set()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


@click.command(
    context_settings={"help_option_names": ["-h", "--help"]},
    help=(
        "Self-hosted Gmail polling AI email agent. Boots the poller "
        "and runs until interrupted."
    ),
)
@click.option(
    "--reset",
    "reset",
    is_flag=True,
    help=(
        "TRUNCATE all data (app tables + LangGraph checkpoints) "
        "before starting the poller. Asks for confirmation unless "
        "-y is also given."
    ),
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    help="Skip the confirmation prompt for --reset.",
)
def main(reset: bool, yes: bool) -> None:
    """CLI entry point. `--reset` wipes every app + LangGraph table
    BEFORE starting so the next boot has no remembered state. Then
    drops into the poller loop."""
    _init_logging()

    if reset:
        if not yes:
            click.confirm(
                "This will TRUNCATE all email analyses, expenses, "
                "polling state, and LangGraph checkpoints. Continue?",
                abort=True,
            )
        reset_database()

    run()


def run() -> None:
    _init_logging()

    stop_event = threading.Event()
    _install_shutdown_handlers(stop_event)

    credentials = get_credentials()
    client = GmailClient(credentials)
    session_factory = get_session_factory()
    notifier = TelegramNotifier()
    notion = _build_notion()

    db_url = _libpq_url(DatabaseSettings().database_url)
    with PostgresSaver.from_conn_string(db_url) as checkpointer:
        checkpointer.setup()
        agent = EmailAgent(
            checkpointer=checkpointer,
            session_factory=session_factory,
            notifier=notifier,
            client=client,
            notion=notion,
        )
        poller = Poller(
            client=client,
            session_factory=session_factory,
            handler=agent.handle_email,
            stop_event=stop_event,
        )
        poller.run()
