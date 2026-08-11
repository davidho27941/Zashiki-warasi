"""Gmail history-based polling — one tick per external invocation.

In v1.0 the daemon loop is gone: an external scheduler (host cron /
k8s CronJob) fires `POST /poll`, which delegates to `tick_once(...)`
below. Per-message dedup via `processed_messages`, per-tick cursor
advance via `gmail_sync_state`. Handler must be idempotent (we rely
on LangGraph's checkpointer keyed by message_id to make this true).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from google.auth.exceptions import RefreshError
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.core.models import GmailSyncState, ProcessedMessage
from zashiki_warasi.core.schemas import EmailMessage, TickResult
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import (
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
    """Gmail poller. Each `tick_once()` = one complete unit of work.

    Holds the wiring (client, session_factory, handler, notifier) that
    every tick needs. `_baseline_if_needed` runs inside `tick_once` so
    a fresh deploy's first `POST /poll` bootstraps the cursor and
    returns cleanly. Subsequent calls skip baseline via the DB row's
    existence.
    """

    def __init__(
        self,
        client: GmailClient,
        session_factory: sessionmaker,
        handler: EmailHandler,
        stop_event: threading.Event | None = None,
        notifier: TelegramNotifier | None = None,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._handler = handler
        self._notifier = notifier
        # Retained for compat with pre-existing tests + graceful early
        # exit inside a long tick if the caller ever sets it. In the
        # FastAPI model no request-scoped code sets it; a fresh Event
        # is fine.
        self.stop_event = stop_event or threading.Event()

    # ----- Public tick surface -----

    def tick_once(self) -> TickResult:
        """Execute exactly one poll cycle and return its outcome.

        Never sleeps, never loops, never emits a heartbeat. All
        recoverable in-tick failures are caught and surfaced via
        `TickResult.error`; the caller decides what to do with the
        result (FastAPI turns it into an HTTP body, CLI prints it).
        Uncaught exceptions propagate — the caller is expected to log
        and translate them (FastAPI's default handler emits HTTP 500).
        """
        started_at = time.monotonic()
        cursor_before: int | None = None
        cursor_after: int | None = None
        processed = 0
        rebaselined = False
        error: str | None = None

        try:
            profile = self._client.get_profile()
        except RefreshError as exc:
            self._notify_credential_failure(exc)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            return TickResult(
                duration_ms=duration_ms,
                messages_processed=0,
                cursor_before=None,
                cursor_after=None,
                rebaselined=False,
                error=f"credential_refresh_failed: {exc}",
            )

        email = profile.email
        baseline_created = self._baseline_if_needed(email, profile.history_id)
        if baseline_created:
            # First-ever tick for this deploy — the row we just wrote
            # IS the cursor; no history to walk yet. Return quickly so
            # the operator (or cron) sees a clean success and the next
            # tick starts consuming real history.
            duration_ms = int((time.monotonic() - started_at) * 1000)
            return TickResult(
                duration_ms=duration_ms,
                messages_processed=0,
                cursor_before=None,
                cursor_after=profile.history_id,
                rebaselined=False,
                error=None,
            )

        try:
            cursor_before, cursor_after, processed = self._run_tick(email)
        except HistoryExpiredError as exc:
            logger.warning(
                f"Gmail history expired at startHistoryId={exc}; "
                "re-baselining"
            )
            new_cursor = self._rebaseline(email)
            duration_ms = int((time.monotonic() - started_at) * 1000)
            return TickResult(
                duration_ms=duration_ms,
                messages_processed=0,
                cursor_before=None,
                cursor_after=new_cursor,
                rebaselined=True,
                error=None,
            )
        except RefreshError as exc:
            self._notify_credential_failure(exc)
            error = f"credential_refresh_failed: {exc}"

        duration_ms = int((time.monotonic() - started_at) * 1000)
        return TickResult(
            duration_ms=duration_ms,
            messages_processed=processed,
            cursor_before=cursor_before,
            cursor_after=cursor_after,
            rebaselined=rebaselined,
            error=error,
        )

    # ----- Internal helpers -----

    def _notify_credential_failure(self, exc: RefreshError) -> None:
        """Log CRITICAL + best-effort Telegram alert. Does NOT raise —
        the caller (tick_once) surfaces the failure via TickResult.error
        so cron sees a normal 200 with an operator-actionable body."""
        message = (
            f"Gmail OAuth refresh failed ({exc}). The refresh token is "
            "expired or revoked. Run `zashiki-warasi reauth` (CLI) or "
            "hit POST /reauth (headless) to re-authorise."
        )
        logger.critical(message)
        if self._notifier is not None:
            try:
                self._notifier.send_message(
                    "🚨 Zashiki-warasi: Gmail 授權失效\n\n"
                    f"{exc}\n\n"
                    "請執行 <code>zashiki-warasi reauth</code> 或呼叫 "
                    "<code>POST /reauth</code> 重新授權。",
                )
            except TelegramError:
                logger.exception(
                    "Failed to send Telegram alert about auth failure"
                )

    def _baseline_if_needed(
        self, email: str, current_history_id: int
    ) -> bool:
        """Return True iff a fresh baseline row was created."""
        with self._session_factory() as session:
            state = session.get(GmailSyncState, email)
            if state is not None:
                return False
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
        return True

    def _run_tick(self, email: str) -> tuple[int, int, int]:
        """Consume Gmail history since the stored cursor.

        Returns (cursor_before, cursor_after, messages_processed).
        Any HistoryExpiredError / RefreshError propagates out — caught
        by `tick_once`'s outer except blocks so they turn into a
        TickResult, not an HTTP 500.
        """
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

        final_cursor = start
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
                final_cursor = state.history_id
        else:
            logger.debug(f"tick: 0 new, cursor unchanged at {start}")

        return start, final_cursor, processed_count

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

    def _rebaseline(self, email: str) -> int:
        """Reset the cursor to the current Gmail historyId. Returns the
        new cursor value."""
        profile = self._client.get_profile()
        with self._session_factory() as session:
            state = session.get(GmailSyncState, email)
            state.history_id = profile.history_id
            session.commit()
        logger.info(
            f"Re-baselined to historyId={profile.history_id} for {email}"
        )
        return profile.history_id


def tick_once(poller: Poller) -> TickResult:
    """Module-level entry point used by the FastAPI `/poll` handler
    and the `tick` CLI subcommand. Thin delegate over `Poller.tick_once`
    so callers don't need to import the class.
    """
    return poller.tick_once()
