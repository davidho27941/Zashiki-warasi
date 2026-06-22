"""Application entry point.

Wires Gmail credentials -> GmailClient -> EmailAgent (with LangGraph
PostgresSaver checkpointer) -> Poller, then blocks on the polling loop.
"""

from __future__ import annotations

import logging

from langgraph.checkpoint.postgres import PostgresSaver

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.core.config import DatabaseSettings
from zashiki_warasi.core.db import get_session_factory
from zashiki_warasi.gmail.auth import get_credentials
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.poller import Poller

logger = logging.getLogger(__name__)


def _libpq_url(sqlalchemy_url: str) -> str:
    """Strip SQLAlchemy's `+psycopg` driver suffix for libpq consumers."""
    return sqlalchemy_url.replace("postgresql+psycopg://", "postgresql://", 1)


def run() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    credentials = get_credentials()
    client = GmailClient(credentials)
    session_factory = get_session_factory()

    db_url = _libpq_url(DatabaseSettings().database_url)
    with PostgresSaver.from_conn_string(db_url) as checkpointer:
        checkpointer.setup()
        agent = EmailAgent(
            checkpointer=checkpointer,
            session_factory=session_factory,
        )
        poller = Poller(
            client=client,
            session_factory=session_factory,
            handler=agent.handle_email,
        )
        poller.run()
