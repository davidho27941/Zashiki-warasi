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
import sys
import threading

import click
import psycopg
import psycopg_pool
from langgraph.checkpoint.postgres import PostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.core.config import (
    DatabaseSettings,
    GmailSettings,
    NotionSettings,
)
from zashiki_warasi.core.db import get_session_factory, reset_database
from zashiki_warasi.core.logging import configure_logging
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import CredentialRefreshError
from zashiki_warasi.gmail.poller import Poller
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.notion_puller import NotionExpensePuller
from zashiki_warasi.notifications.telegram import (
    TelegramError,
    TelegramNotifier,
)

logger = logging.getLogger(__name__)


def _init_logging() -> None:
    """Configure root logging once via `configure_logging()`.

    Thin wrapper kept so that the app's process entry point still owns
    logging bootstrap (rather than callers into it having to remember).
    Idempotent — see `zashiki_warasi.core.logging.configure_logging`.
    """
    configure_logging()


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


@main.command("reauth")
def reauth_cmd() -> None:
    """Delete the cached OAuth token and re-run the InstalledAppFlow.

    Use when the Gmail refresh token has been revoked or expired
    (invalid_grant). Opens a browser to complete authorisation, then
    writes a fresh `token.json`. Exits non-zero if the flow fails
    (e.g. `credentials.json` missing).
    """
    settings = GmailSettings()
    token_path = settings.token_path
    if token_path.exists():
        token_path.unlink()
        click.echo(f"Removed stale token: {token_path}")
    else:
        click.echo(f"No cached token at {token_path}; running flow anyway.")
    creds = get_credentials(settings)
    click.echo(f"Re-authorised; new token written to {token_path}.")
    if not creds.refresh_token:
        click.echo(
            "WARNING: new credentials have no refresh_token — the next "
            "expiry will require re-auth again. Ensure the OAuth client "
            "is a Desktop app and access_type=offline.",
            err=True,
        )


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


EXIT_CREDENTIAL_FAILURE = 78  # sysexits.h EX_CONFIG — user must intervene
EXIT_DB_UNREACHABLE = 71  # sysexits.h EX_OSERR — infra beyond app's control

# psycopg's `application_name` label — surfaces in `pg_stat_activity`
# so the operator can distinguish checkpointer connections from any
# other client (Notion puller, one-shot sync-notion, ad-hoc psql).
_CHECKPOINTER_APPLICATION_NAME = "zashiki-warasi-checkpointer"


def _build_checkpointer_pool(
    settings: DatabaseSettings,
    db_url: str,
    stop_event: threading.Event,
    unreachable_flag: threading.Event,
) -> ConnectionPool:
    """Return an unopened `ConnectionPool` configured for LangGraph.

    The caller is expected to enter it with `with pool: ...` so shutdown
    ordering (pool close AFTER checkpointer exit) is enforced by
    nesting. See design D6.

    `unreachable_flag` is set only by the `reconnect_failed` callback,
    so the caller can distinguish an operator shutdown (Ctrl+C /
    SIGTERM) from an infra-driven shutdown (pool gave up).
    """

    def _configure(conn: psycopg.Connection) -> None:
        # Runs once per newly-created pool connection. Sets the label
        # visible in pg_stat_activity + logs the creation at DEBUG
        # (per level policy in the logging capability). LangGraph's
        # PostgresSaver expects autocommit + dict_row on the pool's
        # kwargs (below); this callback only handles session-level
        # settings that must be applied AFTER connection is open.
        # `SET` is a utility statement and does NOT accept `%s` params
        # (Postgres errors with `syntax error at or near "$1"`), so we
        # go through `set_config()` — a regular function that accepts
        # them and returns the applied value.
        conn.execute(
            "SELECT set_config('application_name', %s, false)",
            (_CHECKPOINTER_APPLICATION_NAME,),
        )
        logger.debug(
            f"checkpointer conn created "
            f"(application_name={_CHECKPOINTER_APPLICATION_NAME})"
        )

    def _check(conn: psycopg.Connection) -> None:
        # On-checkout health check. Delegates to psycopg-pool's builtin
        # (a `SELECT 1`), but wraps it so we can log a WARNING when the
        # pool discards a stale connection. Re-raising is load-bearing:
        # the pool's discard path is triggered by the raised exception.
        # Import path via `psycopg_pool` (not the module-level
        # `ConnectionPool` alias) so tests that monkeypatch
        # `app.ConnectionPool` still exercise the real check.
        try:
            psycopg_pool.ConnectionPool.check_connection(conn)
        except Exception:
            logger.warning(
                "checkpointer pool: discarded stale connection, reconnecting"
            )
            raise

    def _reconnect_failed(pool: ConnectionPool) -> None:
        # Fires when the pool has been unable to establish a connection
        # for `reconnect_timeout` (default 300s). Treated as
        # unrecoverable — parallel to CredentialRefreshError: log
        # CRITICAL, flag the failure so `run()` can exit non-zero, and
        # set the shutdown event so the poller loop returns cleanly.
        logger.critical(
            f"checkpointer pool: reconnect failed after "
            f"{pool.reconnect_timeout}s — DB unreachable"
        )
        unreachable_flag.set()
        stop_event.set()

    return ConnectionPool(
        db_url,
        min_size=settings.checkpointer_pool_min_size,
        max_size=settings.checkpointer_pool_max_size,
        max_lifetime=settings.checkpointer_pool_max_lifetime_seconds,
        max_idle=settings.checkpointer_pool_max_idle_seconds,
        # LangGraph's PostgresSaver requires these on every connection
        # it uses. Passed here so freshly-provisioned pool connections
        # come pre-configured — no per-checkout reconfiguration.
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
        check=_check,
        configure=_configure,
        reconnect_failed=_reconnect_failed,
        open=False,  # caller owns lifecycle via `with pool: ...`
    )


def run() -> None:
    _init_logging()

    stop_event = threading.Event()
    _install_shutdown_handlers(stop_event)

    session_factory = get_session_factory()
    notifier = TelegramNotifier()

    try:
        credentials = get_credentials()
    except CredentialRefreshError as exc:
        _notify_credential_failure(notifier, str(exc))
        logger.critical(f"Aborting startup: {exc}")
        sys.exit(EXIT_CREDENTIAL_FAILURE)

    client = GmailClient(credentials)
    notion = _build_notion()

    _start_notion_sync_thread(NotionSettings(), session_factory, stop_event)

    db_settings = DatabaseSettings()
    db_url = _libpq_url(db_settings.database_url)
    # Dedicated flag so `run()` can distinguish "pool gave up, exit 71"
    # from "operator Ctrl+C, exit 0" without inferring from stop_event.
    db_unreachable = threading.Event()
    pool = _build_checkpointer_pool(
        db_settings, db_url, stop_event, db_unreachable
    )
    try:
        with pool:
            logger.info(
                f"checkpointer pool opened "
                f"(min={db_settings.checkpointer_pool_min_size} "
                f"max={db_settings.checkpointer_pool_max_size} "
                f"max_lifetime={db_settings.checkpointer_pool_max_lifetime_seconds}s "
                f"max_idle={db_settings.checkpointer_pool_max_idle_seconds}s)"
            )
            # PostgresSaver constructed with a pool is NOT a context
            # manager (only `from_conn_string` returns one via
            # @contextmanager). Direct construction returns a bare
            # object whose connections' lifecycle is owned by the pool
            # above — no separate enter/exit needed.
            checkpointer = PostgresSaver(pool)
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
                notifier=notifier,
            )
            try:
                poller.run()
            except CredentialRefreshError:
                # Poller already notified and set stop_event; propagate
                # as non-zero exit so operators know to run `reauth`.
                # Falls through to pool close.
                sys.exit(EXIT_CREDENTIAL_FAILURE)
    finally:
        # Symmetric with the open INFO so `grep 'checkpointer pool'`
        # always shows both endpoints.
        logger.info("checkpointer pool closed")
    if db_unreachable.is_set():
        sys.exit(EXIT_DB_UNREACHABLE)


def _notify_credential_failure(
    notifier: TelegramNotifier, message: str
) -> None:
    """Best-effort Telegram alert at startup credential failure."""
    try:
        notifier.send_message(
            "🚨 Zashiki-warasi: Gmail 授權失效 (啟動時)\n\n"
            f"{message}\n\n"
            "請在主機上執行 <code>zashiki-warasi reauth</code> "
            "重新授權後再啟動。"
        )
    except TelegramError:
        logger.exception(
            "Failed to send Telegram alert about startup auth failure"
        )
