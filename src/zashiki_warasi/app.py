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
from zashiki_warasi.notifications.notion_puller import NotionExpensePuller
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


def _start_notion_sync_thread(
    settings: NotionSettings,
    session_factory,
    stop_event: threading.Event,
) -> threading.Thread | None:
    """Spawn a daemon thread that runs the Notion puller every
    `sync_interval_seconds`. Returns None if the integration is
    disabled or the interval is 0."""
    if not (settings.token and settings.expense_database_id):
        return None
    if settings.sync_interval_seconds == 0:
        logger.info(
            "Notion background sync disabled "
            "(NOTION_SYNC_INTERVAL_SECONDS=0)"
        )
        return None

    puller = NotionExpensePuller(settings, session_factory)
    interval = settings.sync_interval_seconds

    def loop() -> None:
        logger.info(
            f"Notion background sync started (interval={interval}s)"
        )
        while not stop_event.is_set():
            try:
                stats = puller.sync_once()
                if stats.updated or stats.fetched:
                    logger.info(f"Notion sync: {stats}")
            except Exception:
                # Best-effort: a transient Notion 5xx must not kill the
                # poller. We log and retry on the next tick.
                logger.exception(
                    "Notion sync failed; will retry next tick"
                )
            stop_event.wait(interval)
        logger.info("Notion background sync stopped")

    thread = threading.Thread(target=loop, name="notion-sync", daemon=True)
    thread.start()
    return thread


@click.group(
    context_settings={"help_option_names": ["-h", "--help"]},
    invoke_without_command=True,
    help=(
        "Self-hosted Gmail polling AI email agent. Run with no "
        "subcommand to boot the poller; use a subcommand for one-shot "
        "operations."
    ),
)
@click.option(
    "--reset",
    "reset",
    is_flag=True,
    help=(
        "TRUNCATE all data (app tables + LangGraph checkpoints) "
        "before starting the poller. Asks for confirmation unless "
        "-y is also given. Only valid without a subcommand."
    ),
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    help="Skip the confirmation prompt for --reset.",
)
@click.pass_context
def main(ctx: click.Context, reset: bool, yes: bool) -> None:
    """CLI entry point. With no subcommand: starts the poller (with
    optional `--reset`). With a subcommand: runs that one-shot
    operation and exits."""
    _init_logging()

    if ctx.invoked_subcommand is not None:
        if reset:
            raise click.UsageError(
                "--reset is only valid without a subcommand."
            )
        return

    if reset:
        if not yes:
            click.confirm(
                "This will TRUNCATE all email analyses, expenses, "
                "polling state, and LangGraph checkpoints. Continue?",
                abort=True,
            )
        reset_database()

    run()


@main.command("sync-notion")
def sync_notion_cmd() -> None:
    """Run a single Notion→DB sync pass and exit.

    Useful for debugging or manual catch-up after the background
    thread has been disabled (NOTION_SYNC_INTERVAL_SECONDS=0).
    Exits non-zero on any sync error.
    """
    settings = NotionSettings()
    if not settings.token or not settings.expense_database_id:
        raise click.ClickException(
            "NOTION_TOKEN / NOTION_EXPENSE_DATABASE_ID not set."
        )
    puller = NotionExpensePuller(settings, get_session_factory())
    stats = puller.sync_once()
    click.echo(f"Notion sync complete: {stats}")


def run() -> None:
    _init_logging()

    stop_event = threading.Event()
    _install_shutdown_handlers(stop_event)

    credentials = get_credentials()
    client = GmailClient(credentials)
    session_factory = get_session_factory()
    notifier = TelegramNotifier()
    notion = _build_notion()

    _start_notion_sync_thread(NotionSettings(), session_factory, stop_event)

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
