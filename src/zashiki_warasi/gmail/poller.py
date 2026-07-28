"""Gmail history-based polling loop.

Per-message dedup via `processed_messages`, per-tick cursor advance
via `gmail_sync_state`. Handler must be idempotent (we rely on
LangGraph's checkpointer keyed by message_id to make this true).
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, NoReturn

from google.auth.exceptions import RefreshError
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.core.models import GmailSyncState, ProcessedMessage
from zashiki_warasi.core.schemas import EmailMessage
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import (
    CredentialRefreshError,
    HistoryExpiredError,
    MessageNotFoundError,
)
from zashiki_warasi.notifications.telegram import (
    TelegramError,
    TelegramNotifier,
)

logger = logging.getLogger(__name__)

EmailHandler = Callable[[EmailMessage], None]


class Poller:
    """Long-running Gmail poller driven by Gmail's historyId cursor.

    On every tick:
      - read last historyId from gmail_sync_state
      - list new message IDs since that point
      - for each, skip if already in processed_messages
        else fetch + invoke handler + record in processed_messages
      - advance gmail_sync_state.history_id once the whole tick succeeds

    Crash safety: per-message dedup means already-handled messages are
    skipped on restart; per-tick cursor advance means a failure inside
    the tick keeps the cursor still and the batch retries.
    """

    def __init__(
        self,
        client: GmailClient,
        session_factory: sessionmaker,
        handler: EmailHandler,
        interval_seconds: int = 30,
        stop_event: threading.Event | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._handler = handler
        self._interval = interval_seconds
        self._notifier = notifier
        # Exposed publicly so the app entry point can `stop_event.set()`
        # from a signal handler without holding a reference to a private
        # attribute.
        self.stop_event = stop_event or threading.Event()

    def run(self) -> None:
        """Poll Gmail until `stop_event` is set, then return cleanly.

        Shutdown points:
          - before each tick
          - between messages within a tick
          - during the inter-tick sleep (Event.wait wakes immediately
            when the event is set)

        The message currently being processed always runs to completion
        so its `processed_messages` row is written before exit.

        A dead OAuth refresh token (google.auth RefreshError) is
        unrecoverable in-process: we notify (best-effort), stop the
        loop, and re-raise as `CredentialRefreshError` so the app entry
        point exits non-zero.
        """
        try:
            profile = self._client.get_profile()
        except RefreshError as exc:
            self._handle_credential_failure(exc)
        email = profile.email
        logger.info(f"Poller starting for {email}")
        self._baseline_if_needed(email, profile.history_id)

        while not self.stop_event.is_set():
            try:
                self._tick(email)
            except HistoryExpiredError as exc:
                logger.warning(
                    f"Gmail history expired at startHistoryId={exc}; "
                    "re-baselining"
                )
                self._rebaseline(email)
            except RefreshError as exc:
                self._handle_credential_failure(exc)
            except Exception:
                logger.exception("Unhandled error during tick; will retry")
            if self.stop_event.wait(timeout=self._interval):
                break

        logger.info(f"Poller stopped for {email}")

    def _handle_credential_failure(
        self, exc: RefreshError
    ) -> NoReturn:
        """Notify + stop the loop, then re-raise CredentialRefreshError."""
        message = (
            f"Gmail OAuth refresh failed ({exc}). The refresh token is "
            "expired or revoked; the poller will exit. Run "
            "`zashiki-warasi reauth` to re-authorise."
        )
        logger.critical(message)
        if self._notifier is not None:
            try:
                self._notifier.send_message(
                    "🚨 Zashiki-warasi: Gmail 授權失效\n\n"
                    f"{exc}\n\n"
                    "已停止 poller。請在主機上執行 "
                    "<code>zashiki-warasi reauth</code> 重新授權。",
                )
            except TelegramError:
                logger.exception(
                    "Failed to send Telegram alert about auth failure"
                )
        self.stop_event.set()
        raise CredentialRefreshError(message) from exc

    # ----- Branch A: first-run baseline -----

    def _baseline_if_needed(
        self, email: str, current_history_id: int
    ) -> None:
        with self._session_factory() as session:
            state = session.get(GmailSyncState, email)
            if state is not None:
                logger.info(
                    f"Resuming from historyId={state.history_id} for {email}"
                )
                return
            session.add(
                GmailSyncState(
                    email_address=email,
                    history_id=current_history_id,
                )
            )
            session.commit()
            logger.info(
                f"First run: baseline at historyId={current_history_id} "
                f"for {email} (backlog skipped)"
            )

    # ----- Branch D: normal tick -----

    def _tick(self, email: str) -> None:
        with self._session_factory() as session:
            state = session.get(GmailSyncState, email)
            start = state.history_id

        max_history_id = start
        processed_count = 0
        for msg_id in self._client.list_history(start):
            if self.stop_event.is_set():
                logger.info(
                    "Stop requested; aborting tick before next message"
                )
                break
            message = self._process_message(msg_id)
            if message is not None:
                processed_count += 1
                if message.history_id > max_history_id:
                    max_history_id = message.history_id

        if max_history_id > start:
            with self._session_factory() as session:
                state = session.get(GmailSyncState, email)
                if max_history_id > state.history_id:
                    state.history_id = max_history_id
                    session.commit()
                    logger.info(
                        f"tick: {processed_count} new messages, cursor "
                        f"{start} -> {max_history_id}"
                    )
        else:
            # Steady-state "loop alive" heartbeat — INFO would flood
            # (30s cadence × 2880/day just for zero-op ticks). DEBUG
            # keeps it visible when explicitly enabled.
            logger.debug(f"tick: 0 new, cursor unchanged at {start}")

    def _process_message(self, msg_id: str) -> EmailMessage | None:
        # D1: already processed
        with self._session_factory() as session:
            if session.get(ProcessedMessage, msg_id) is not None:
                return None

        # D2: message gone (deleted between history event and fetch)
        try:
            message = self._client.get_message(msg_id)
        except MessageNotFoundError:
            logger.info(
                f"Message {msg_id} not found (deleted); marking processed"
            )
            self._mark_processed(msg_id)
            return None

        # D3: hand to handler, then record dedup.
        # Handler may be slow (LLM); keep it OUTSIDE any open transaction
        # so we don't hold a DB connection for the LLM call's duration.
        self._handler(message)
        self._mark_processed(msg_id)
        return message

    def _mark_processed(self, msg_id: str) -> None:
        with self._session_factory() as session:
            session.add(ProcessedMessage(message_id=msg_id))
            session.commit()

    # ----- Branch C: history retention exceeded -----

    def _rebaseline(self, email: str) -> None:
        profile = self._client.get_profile()
        with self._session_factory() as session:
            state = session.get(GmailSyncState, email)
            state.history_id = profile.history_id
            session.commit()
        logger.info(
            f"Re-baselined to historyId={profile.history_id} for {email}"
        )
