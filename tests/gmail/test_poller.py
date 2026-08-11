"""Behavioural tests for the Gmail Poller.

Mocks `GmailClient` and the handler; uses in-memory SQLite for the
session factory. Each test exercises one of the documented branches
(A first-run baseline, B resume, C rebaseline, D1 dedup, D2 deleted,
D3 normal), the per-tick cursor advance contract, or the v1.0
`tick_once` return shape / credential-failure handling.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from google.auth.exceptions import RefreshError

from zashiki_warasi.core.models import (
    Base,
    GmailSyncState,
    ProcessedMessage,
)
from zashiki_warasi.core.schemas import EmailMessage, ProfileInfo, TickResult
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import (
    HistoryExpiredError,
    MessageNotFoundError,
)
from zashiki_warasi.gmail.poller import Poller, tick_once
from zashiki_warasi.notifications.telegram import TelegramError


EMAIL = "user@example.com"


# ---------- fixtures ----------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock(spec=GmailClient)
    client.get_profile.return_value = ProfileInfo(
        email=EMAIL, history_id=1000
    )
    client.list_history.return_value = iter([])
    return client


@pytest.fixture
def mock_handler() -> MagicMock:
    return MagicMock(name="handler")


@pytest.fixture
def poller(mock_client, session_factory, mock_handler) -> Poller:
    return Poller(
        client=mock_client,
        session_factory=session_factory,
        handler=mock_handler,
    )


def _make_email(msg_id: str, history_id: int, **overrides) -> EmailMessage:
    defaults = dict(
        id=msg_id,
        thread_id=f"t-{msg_id}",
        history_id=history_id,
        from_address="sender@example.com",
        subject="Test",
        snippet="snippet",
        body_plain="body",
        received_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
    )
    defaults.update(overrides)
    return EmailMessage(**defaults)


def _read_state(session_factory) -> GmailSyncState | None:
    with session_factory() as session:
        return session.get(GmailSyncState, EMAIL)


def _processed_ids(session_factory) -> set[str]:
    with session_factory() as session:
        rows = session.scalars(select(ProcessedMessage.message_id)).all()
        return set(rows)


def _prime_state(session_factory, history_id: int) -> None:
    with session_factory() as session:
        session.add(
            GmailSyncState(email_address=EMAIL, history_id=history_id)
        )
        session.commit()


# ---------- Branch A: first-run baseline ----------


class TestBranchABaseline:
    def test_inserts_sync_state_when_missing(self, poller, session_factory):
        created = poller._baseline_if_needed(EMAIL, current_history_id=1500)

        assert created is True
        state = _read_state(session_factory)
        assert state is not None
        assert state.email_address == EMAIL
        assert state.history_id == 1500

    def test_handler_not_called_during_baseline(
        self, poller, mock_handler
    ):
        poller._baseline_if_needed(EMAIL, current_history_id=1500)
        mock_handler.assert_not_called()

    def test_returns_false_when_state_already_exists(
        self, poller, session_factory
    ):
        _prime_state(session_factory, 500)
        created = poller._baseline_if_needed(EMAIL, current_history_id=9999)
        assert created is False


# ---------- Branch B: resume from existing state ----------


class TestBranchBResume:
    def test_does_not_overwrite_existing_state(
        self, poller, session_factory
    ):
        _prime_state(session_factory, 500)

        poller._baseline_if_needed(EMAIL, current_history_id=9999)

        state = _read_state(session_factory)
        assert state.history_id == 500  # unchanged


# ---------- Branch C: history expired -> rebaseline ----------


class TestBranchCRebaseline:
    def test_rebaseline_updates_history_id_to_current_profile(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.get_profile.return_value = ProfileInfo(
            email=EMAIL, history_id=2000
        )

        new_cursor = poller._rebaseline(EMAIL)

        assert new_cursor == 2000
        assert _read_state(session_factory).history_id == 2000

    def test_history_expired_bubbles_up_from_run_tick(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.side_effect = HistoryExpiredError(500)

        with pytest.raises(HistoryExpiredError):
            poller._run_tick(EMAIL)

    def test_tick_once_catches_history_expired_and_rebaselines(
        self, poller, session_factory, mock_client
    ):
        """tick_once() should NEVER let HistoryExpiredError escape;
        it rebaselines and returns TickResult(rebaselined=True)."""
        _prime_state(session_factory, 500)
        # get_profile is called twice: baseline check + rebaseline.
        # First returns the profile as expected; second is used for
        # rebaselining after the expired error.
        mock_client.get_profile.return_value = ProfileInfo(
            email=EMAIL, history_id=2500
        )
        mock_client.list_history.side_effect = HistoryExpiredError(500)

        result = poller.tick_once()

        assert result.rebaselined is True
        assert result.error is None
        assert result.cursor_after == 2500
        assert _read_state(session_factory).history_id == 2500


# ---------- Branch D1: already processed (dedup skip) ----------


class TestBranchD1AlreadyProcessed:
    def test_skips_message_already_in_processed_messages(
        self, poller, session_factory, mock_client, mock_handler
    ):
        _prime_state(session_factory, 500)
        with session_factory() as session:
            session.add(ProcessedMessage(message_id="msg-1"))
            session.commit()
        mock_client.list_history.return_value = iter(["msg-1"])

        poller._run_tick(EMAIL)

        mock_client.get_message.assert_not_called()
        mock_handler.assert_not_called()
        assert _read_state(session_factory).history_id == 500


# ---------- Branch D2: message not found (deleted) ----------


class TestBranchD2MessageNotFound:
    def test_marks_deleted_message_as_processed(
        self, poller, session_factory, mock_client, mock_handler
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(["msg-deleted"])
        mock_client.get_message.side_effect = MessageNotFoundError(
            "msg-deleted"
        )

        poller._run_tick(EMAIL)

        mock_handler.assert_not_called()
        assert "msg-deleted" in _processed_ids(session_factory)

    def test_cursor_not_advanced_for_deleted_only_tick(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(["msg-deleted"])
        mock_client.get_message.side_effect = MessageNotFoundError(
            "msg-deleted"
        )

        poller._run_tick(EMAIL)

        assert _read_state(session_factory).history_id == 500


# ---------- Branch D3: normal flow ----------


class TestBranchD3Normal:
    def test_handler_called_with_parsed_email(
        self, poller, session_factory, mock_client, mock_handler
    ):
        _prime_state(session_factory, 500)
        email = _make_email("msg-1", history_id=600)
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = email

        poller._run_tick(EMAIL)

        mock_handler.assert_called_once_with(email)

    def test_message_recorded_in_processed_messages(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = _make_email("msg-1", 600)

        poller._run_tick(EMAIL)

        assert "msg-1" in _processed_ids(session_factory)

    def test_cursor_advances_to_message_history_id(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = _make_email("msg-1", 600)

        poller._run_tick(EMAIL)

        assert _read_state(session_factory).history_id == 600


# ---------- per-tick cursor advance contract ----------


class TestCursorAdvance:
    def test_advances_to_max_history_id_in_batch(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(
            ["msg-1", "msg-2", "msg-3"]
        )
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 700),
            _make_email("msg-3", 650),
        ]

        poller._run_tick(EMAIL)

        assert _read_state(session_factory).history_id == 700

    def test_no_advance_when_no_new_messages(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter([])

        poller._run_tick(EMAIL)

        assert _read_state(session_factory).history_id == 500

    def test_handler_failure_aborts_tick_and_blocks_cursor(
        self, poller, session_factory, mock_client, mock_handler
    ):
        """Partial failure: handler raises on the second message.

        Verifies:
          - first message was processed before the failure
          - second message did NOT get marked processed
          - cursor stayed put (so the batch retries next tick)
          - RuntimeError propagates (v1.0 FastAPI turns it into HTTP 500)
        """
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(["msg-1", "msg-2"])
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 700),
        ]
        mock_handler.side_effect = [None, RuntimeError("LLM broke")]

        with pytest.raises(RuntimeError, match="LLM broke"):
            poller._run_tick(EMAIL)

        processed = _processed_ids(session_factory)
        assert "msg-1" in processed
        assert "msg-2" not in processed
        assert _read_state(session_factory).history_id == 500


# ---------- _process_message in isolation ----------


class TestProcessMessage:
    def test_returns_email_on_success(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        email = _make_email("msg-1", 600)
        mock_client.get_message.return_value = email

        result = poller._process_message("msg-1")

        assert result is email

    def test_returns_none_for_already_processed(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(ProcessedMessage(message_id="msg-1"))
            session.commit()

        result = poller._process_message("msg-1")

        assert result is None
        mock_client.get_message.assert_not_called()

    def test_returns_none_for_deleted_message(
        self, poller, mock_client
    ):
        mock_client.get_message.side_effect = MessageNotFoundError(
            "msg-x"
        )
        assert poller._process_message("msg-x") is None


# ---------- graceful shutdown (stop_event) ----------


class TestGracefulShutdown:
    """v1.0 no longer has a loop, but stop_event is retained so a
    long-running tick can be interrupted between messages if the
    caller sets it (e.g. a future SIGTERM in the FastAPI lifespan)."""

    def test_default_stop_event_created_when_none_passed(
        self, mock_client, session_factory, mock_handler
    ):
        import threading

        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
        )
        assert isinstance(poller.stop_event, threading.Event)
        assert not poller.stop_event.is_set()

    def test_external_stop_event_is_used(
        self, mock_client, session_factory, mock_handler
    ):
        import threading

        event = threading.Event()
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
            stop_event=event,
        )
        assert poller.stop_event is event

    def test_tick_stops_between_messages_when_event_set(
        self, mock_client, session_factory, mock_handler
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(
            ["msg-1", "msg-2", "msg-3"]
        )
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 601),
            _make_email("msg-3", 602),
        ]

        import threading

        event = threading.Event()
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
            stop_event=event,
        )
        # Set stop after the handler processes msg-1.
        mock_handler.side_effect = lambda _email: event.set()

        poller._run_tick(EMAIL)

        # msg-1 fully processed; msg-2 and msg-3 skipped.
        assert mock_handler.call_count == 1
        assert "msg-1" in _processed_ids(session_factory)
        assert "msg-2" not in _processed_ids(session_factory)


# ---------- credential failure handling ----------


class TestCredentialFailure:
    """v1.0 catches RefreshError inside tick_once and surfaces it via
    TickResult.error (with notify + CRITICAL log side-effects), rather
    than raising CredentialRefreshError like v0.6.x did. The FastAPI
    handler then returns HTTP 200 with the error field populated —
    operators still see the failure via Telegram + response body."""

    def test_startup_refresh_error_produces_tickresult_error(
        self, mock_client, session_factory, mock_handler
    ):
        mock_client.get_profile.side_effect = RefreshError(
            "invalid_grant: revoked"
        )
        notifier = MagicMock()
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
            notifier=notifier,
        )

        result = poller.tick_once()

        assert isinstance(result, TickResult)
        assert result.error is not None
        assert "credential_refresh_failed" in result.error
        notifier.send_message.assert_called_once()

    def test_mid_tick_refresh_error_produces_tickresult_error(
        self, mock_client, session_factory, mock_handler
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.side_effect = RefreshError(
            "invalid_grant: token revoked mid-tick"
        )
        notifier = MagicMock()
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
            notifier=notifier,
        )

        result = poller.tick_once()

        assert result.error is not None
        assert "credential_refresh_failed" in result.error
        notifier.send_message.assert_called_once()
        alert = notifier.send_message.call_args[0][0]
        assert "reauth" in alert
        assert "Gmail" in alert

    def test_refresh_error_without_notifier_is_silent_but_surfaced(
        self, mock_client, session_factory, mock_handler
    ):
        mock_client.get_profile.side_effect = RefreshError("invalid_grant")
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
        )

        result = poller.tick_once()

        assert result.error is not None
        assert "credential_refresh_failed" in result.error

    def test_refresh_error_survives_telegram_error(
        self, mock_client, session_factory, mock_handler
    ):
        """If Telegram is down, tick_once still returns a TickResult
        with the error field populated — losing the alert is bad, but
        the operator still sees the failure in the /poll response."""
        mock_client.get_profile.side_effect = RefreshError("invalid_grant")
        notifier = MagicMock()
        notifier.send_message.side_effect = TelegramError("api down")
        poller = Poller(
            client=mock_client,
            session_factory=session_factory,
            handler=mock_handler,
            notifier=notifier,
        )

        result = poller.tick_once()

        assert result.error is not None


# ---------- tick_once return shape ----------


class TestTickOnce:
    """The v1.0 tick_once contract: always returns a TickResult, never
    raises for handled failures, populates duration_ms + counts +
    cursor deltas so cron / operator can debug without tailing logs."""

    def test_empty_inbox_returns_zero_processed(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter([])

        result = poller.tick_once()

        assert isinstance(result, TickResult)
        assert result.messages_processed == 0
        assert result.cursor_before == 500
        assert result.cursor_after == 500
        assert result.rebaselined is False
        assert result.error is None
        assert result.duration_ms >= 0

    def test_processes_batch_and_advances_cursor(
        self, poller, session_factory, mock_client
    ):
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter(
            ["msg-1", "msg-2", "msg-3"]
        )
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 700),
            _make_email("msg-3", 650),
        ]

        result = poller.tick_once()

        assert result.messages_processed == 3
        assert result.cursor_before == 500
        assert result.cursor_after == 700
        assert result.rebaselined is False
        assert result.error is None

    def test_first_run_baseline_returns_clean_result(
        self, poller, session_factory, mock_client
    ):
        """No existing sync_state row → baseline gets created + tick
        returns immediately without walking history."""
        mock_client.get_profile.return_value = ProfileInfo(
            email=EMAIL, history_id=1234
        )

        result = poller.tick_once()

        assert result.messages_processed == 0
        assert result.cursor_before is None
        assert result.cursor_after == 1234
        assert result.rebaselined is False
        assert result.error is None
        # list_history NOT called on baseline turn — the row we just
        # wrote IS the cursor, there's no history to consume yet.
        mock_client.list_history.assert_not_called()
        assert _read_state(session_factory).history_id == 1234

    def test_module_level_tick_once_delegates_to_poller(
        self, poller, session_factory, mock_client
    ):
        """tick_once(poller) is the entry point FastAPI /poll uses —
        a thin delegate so callers don't need to import the class."""
        _prime_state(session_factory, 500)
        mock_client.list_history.return_value = iter([])

        result = tick_once(poller)

        assert isinstance(result, TickResult)
        assert result.messages_processed == 0
