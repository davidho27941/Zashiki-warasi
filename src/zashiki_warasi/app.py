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

from langgraph.checkpoint.postgres import PostgresSaver

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.core.config import DatabaseSettings
from zashiki_warasi.core.db import get_session_factory
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.poller import Poller
from zashiki_warasi.notifications.telegram import TelegramNotifier

logger = logging.getLogger(__name__)


def _libpq_url(sqlalchemy_url: str) -> str:
    """Strip SQLAlchemy's `+psycopg` driver suffix for libpq consumers."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


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


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    stop_event = threading.Event()
    _install_shutdown_handlers(stop_event)

    credentials = get_credentials()
    client = GmailClient(credentials)
    session_factory = get_session_factory()
    notifier = TelegramNotifier()

    db_url = _libpq_url(DatabaseSettings().database_url)
    with PostgresSaver.from_conn_string(db_url) as checkpointer:
        checkpointer.setup()
        agent = EmailAgent(
            checkpointer=checkpointer,
            session_factory=session_factory,
            notifier=notifier,
        )
        poller = Poller(
            client=client,
            session_factory=session_factory,
            handler=agent.handle_email,
            stop_event=stop_event,
        )
        poller.run()
