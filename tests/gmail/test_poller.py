"""Behavioural tests for the Gmail Poller.

Mocks `GmailClient` and the handler; uses in-memory SQLite for the
session factory. Each test exercises one of the documented branches
(A first-run baseline, B resume, C rebaseline, D1 dedup, D2 deleted,
D3 normal) or the per-tick cursor advance contract.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.core.models import (
    Base,
    GmailSyncState,
    ProcessedMessage,
)
from zashiki_warasi.core.schemas import EmailMessage, ProfileInfo
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import (
    HistoryExpiredError,
    MessageNotFoundError,
)
from zashiki_warasi.gmail.poller import Poller


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
        interval_seconds=1,
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


# ---------- Branch A: first-run baseline ----------


class TestBranchABaseline:
    def test_inserts_sync_state_when_missing(self, poller, session_factory):
        poller._baseline_if_needed(EMAIL, current_history_id=1500)

        state = _read_state(session_factory)
        assert state is not None
        assert state.email_address == EMAIL
        assert state.history_id == 1500

    def test_handler_not_called_during_baseline(
        self, poller, mock_handler
    ):
        poller._baseline_if_needed(EMAIL, current_history_id=1500)
        mock_handler.assert_not_called()


# ---------- Branch B: resume from existing state ----------


class TestBranchBResume:
    def test_does_not_overwrite_existing_state(
        self, poller, session_factory
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()

        poller._baseline_if_needed(EMAIL, current_history_id=9999)

        state = _read_state(session_factory)
        assert state.history_id == 500  # unchanged


# ---------- Branch C: history expired -> rebaseline ----------


class TestBranchCRebaseline:
    def test_rebaseline_updates_history_id_to_current_profile(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.get_profile.return_value = ProfileInfo(
            email=EMAIL, history_id=2000
        )

        poller._rebaseline(EMAIL)

        state = _read_state(session_factory)
        assert state.history_id == 2000

    def test_tick_propagates_history_expired(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.side_effect = HistoryExpiredError(500)

        with pytest.raises(HistoryExpiredError):
            poller._tick(EMAIL)


# ---------- Branch D1: already processed (dedup skip) ----------


class TestBranchD1AlreadyProcessed:
    def test_skips_message_already_in_processed_messages(
        self, poller, session_factory, mock_client, mock_handler
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.add(ProcessedMessage(message_id="msg-1"))
            session.commit()
        mock_client.list_history.return_value = iter(["msg-1"])

        poller._tick(EMAIL)

        mock_client.get_message.assert_not_called()
        mock_handler.assert_not_called()
        # cursor unchanged
        assert _read_state(session_factory).history_id == 500


# ---------- Branch D2: message not found (deleted) ----------


class TestBranchD2MessageNotFound:
    def test_marks_deleted_message_as_processed(
        self, poller, session_factory, mock_client, mock_handler
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(["msg-deleted"])
        mock_client.get_message.side_effect = MessageNotFoundError(
            "msg-deleted"
        )

        poller._tick(EMAIL)

        mock_handler.assert_not_called()
        assert "msg-deleted" in _processed_ids(session_factory)

    def test_cursor_not_advanced_for_deleted_only_tick(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(["msg-deleted"])
        mock_client.get_message.side_effect = MessageNotFoundError(
            "msg-deleted"
        )

        poller._tick(EMAIL)

        assert _read_state(session_factory).history_id == 500


# ---------- Branch D3: normal flow ----------


class TestBranchD3Normal:
    def test_handler_called_with_parsed_email(
        self, poller, session_factory, mock_client, mock_handler
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        email = _make_email("msg-1", history_id=600)
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = email

        poller._tick(EMAIL)

        mock_handler.assert_called_once_with(email)

    def test_message_recorded_in_processed_messages(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = _make_email("msg-1", 600)

        poller._tick(EMAIL)

        assert "msg-1" in _processed_ids(session_factory)

    def test_cursor_advances_to_message_history_id(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(["msg-1"])
        mock_client.get_message.return_value = _make_email("msg-1", 600)

        poller._tick(EMAIL)

        assert _read_state(session_factory).history_id == 600


# ---------- per-tick cursor advance contract ----------


class TestCursorAdvance:
    def test_advances_to_max_history_id_in_batch(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(
            ["msg-1", "msg-2", "msg-3"]
        )
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 700),
            _make_email("msg-3", 650),
        ]

        poller._tick(EMAIL)

        assert _read_state(session_factory).history_id == 700

    def test_no_advance_when_no_new_messages(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter([])

        poller._tick(EMAIL)

        assert _read_state(session_factory).history_id == 500

    def test_handler_failure_aborts_tick_and_blocks_cursor(
        self, poller, session_factory, mock_client, mock_handler
    ):
        """Partial failure: handler raises on the second message.

        Verifies:
          - first message was processed before the failure
          - second message did NOT get marked processed
          - cursor stayed put (so the batch retries next tick)
        """
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
        mock_client.list_history.return_value = iter(["msg-1", "msg-2"])
        mock_client.get_message.side_effect = [
            _make_email("msg-1", 600),
            _make_email("msg-2", 700),
        ]
        mock_handler.side_effect = [None, RuntimeError("LLM broke")]

        with pytest.raises(RuntimeError, match="LLM broke"):
            poller._tick(EMAIL)

        processed = _processed_ids(session_factory)
        assert "msg-1" in processed
        assert "msg-2" not in processed
        assert _read_state(session_factory).history_id == 500


# ---------- _process_message in isolation ----------


class TestProcessMessage:
    def test_returns_email_on_success(
        self, poller, session_factory, mock_client
    ):
        with session_factory() as session:
            session.add(
                GmailSyncState(email_address=EMAIL, history_id=500)
            )
            session.commit()
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
