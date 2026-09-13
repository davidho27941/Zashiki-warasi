"""v1.4: CalendarSubgraph — extract node + create node with mocks."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.agents.verticals.calendar import CalendarSubgraph
from zashiki_warasi.calendar.client import (
    BusySlot,
    CalendarError,
    CalendarScopeNotGranted,
    ExistingEvent,
    InsertedEvent,
    iCalUidExists,
)
from zashiki_warasi.core.schemas import (
    AttachmentMeta,
    CalendarCreated,
    CalendarDuplicate,
    CalendarEventDraft,
    CalendarSkipped,
    EmailMessage,
)


# ---------- fixtures ----------
#
# NOTE: no `_reset_scope_flag` fixture needed — the "logged 403 once"
# latch is now an instance attribute on `CalendarSubgraph`, so each
# `_sg(...)` call in these tests gets a fresh, un-tripped latch.


@pytest.fixture
def fake_email() -> EmailMessage:
    return EmailMessage(
        id="msg-cal-1",
        thread_id="t",
        history_id=100,
        from_address="invite@example.com",
        subject="會議邀請:產品週會",
        body_plain="請確認 2026-09-15 14:00-15:00 產品週會於信義區辦公室。",
        received_at=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        attachments=[],
    )


@pytest.fixture
def fake_email_with_ics() -> EmailMessage:
    return EmailMessage(
        id="msg-cal-ics",
        thread_id="t",
        history_id=100,
        from_address="invite@example.com",
        subject="會議邀請",
        body_plain="See attached invite.",
        received_at=datetime(2026, 9, 12, 10, 0, tzinfo=timezone.utc),
        attachments=[
            AttachmentMeta(
                attachment_id="att-1",
                filename="invite.ics",
                mime_type="text/calendar",
                size=400,
            )
        ],
    )


def _draft() -> CalendarEventDraft:
    return CalendarEventDraft(
        title="產品週會",
        start=datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
        location="信義區辦公室",
        ical_uid="ics-uid@example.com",
    )


def _build_model_returning(draft: CalendarEventDraft | None) -> MagicMock:
    structured = MagicMock(name="structured")
    structured.invoke.return_value = draft
    model = MagicMock(name="chat_model")
    model.with_structured_output.return_value = structured
    return model


def _mock_gmail_client_returning_ics(ics_bytes: bytes) -> MagicMock:
    client = MagicMock(name="gmail_client")
    client.get_attachment.return_value = ics_bytes
    return client


def _mock_calendar_client(
    *,
    busy: list[BusySlot] | None = None,
    events: list[ExistingEvent] | None = None,
    insert_result: InsertedEvent | Exception | None = None,
    freebusy_error: Exception | None = None,
) -> MagicMock:
    client = MagicMock(name="calendar_client")
    if freebusy_error is not None:
        client.check_free_busy.side_effect = freebusy_error
    else:
        client.check_free_busy.return_value = busy or []
    client.list_events_in_window.return_value = events or []
    if isinstance(insert_result, Exception):
        client.insert_event.side_effect = insert_result
    else:
        client.insert_event.return_value = insert_result or InsertedEvent(
            id="gcal-abc",
            ical_uid="test@zashiki.local",
            view_url="https://calendar.google.com/xxx",
        )
    return client


def _sg(model, gmail_client, calendar_client) -> CalendarSubgraph:
    return CalendarSubgraph(
        checkpointer=None,
        client=gmail_client,
        calendar_client=calendar_client,
        model=model,
    )


# ---------- _extract_node -----------------------------------------------


class TestExtract:
    def test_llm_path_when_no_ics(self, fake_email):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())
        out = sg._extract_node({"email": fake_email, "analysis": None, "side_effect": None, "extracted": None})
        assert out["extracted"] is not None
        assert out["extracted"].title == "產品週會"

    def test_ics_path_skips_llm(self, fake_email_with_ics):
        ics_bytes = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:ics-real@example.com
DTSTART;TZID=Asia/Taipei:20260915T140000
DTEND;TZID=Asia/Taipei:20260915T150000
SUMMARY:Real Meeting from ICS
LOCATION:Taipei
END:VEVENT
END:VCALENDAR
"""
        model = _build_model_returning(None)  # would return None if called
        sg = _sg(model, _mock_gmail_client_returning_ics(ics_bytes), _mock_calendar_client())

        out = sg._extract_node({"email": fake_email_with_ics, "analysis": None, "side_effect": None, "extracted": None})
        assert out["extracted"] is not None
        assert out["extracted"].title == "Real Meeting from ICS"
        assert out["extracted"].ical_uid == "ics-real@example.com"
        # LLM structured model must NOT have been invoked
        model.with_structured_output.return_value.invoke.assert_not_called()

    def test_ics_parse_failure_falls_through_to_llm(self, fake_email_with_ics):
        model = _build_model_returning(_draft())
        sg = _sg(model, _mock_gmail_client_returning_ics(b"garbage"), _mock_calendar_client())

        out = sg._extract_node({"email": fake_email_with_ics, "analysis": None, "side_effect": None, "extracted": None})
        # Falls through to LLM which returns _draft()
        assert out["extracted"] is not None
        assert out["extracted"].title == "產品週會"

    def test_empty_body_and_no_ics_skips(self, fake_email):
        empty_email = fake_email.model_copy(update={"body_plain": "", "body_html": None, "snippet": ""})
        model = _build_model_returning(None)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({"email": empty_email, "analysis": None, "side_effect": None, "extracted": None})
        assert out["extracted"] is None
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "extraction_failed"

    def test_llm_returns_none_skips(self, fake_email):
        model = _build_model_returning(None)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({"email": fake_email, "analysis": None, "side_effect": None, "extracted": None})
        assert out["extracted"] is None
        assert isinstance(out["side_effect"], CalendarSkipped)


# ---------- _create_node ------------------------------------------------


class TestCreate:
    def test_success_no_conflicts_returns_calendar_created(self, fake_email):
        calendar_client = _mock_calendar_client(
            busy=[],
            insert_result=InsertedEvent(
                id="gcal-new",
                ical_uid="ics-uid@example.com",
                view_url="https://calendar.google.com/xxx",
            ),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        assert out["side_effect"].event_id == "gcal-new"
        assert out["side_effect"].conflicts == []
        # Free/busy called with tentative time window
        calendar_client.check_free_busy.assert_called_once()

    def test_conflicts_included_in_side_effect(self, fake_email):
        conflict = ExistingEvent(
            id="existing",
            title="週會",
            start=datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc),
        )
        calendar_client = _mock_calendar_client(
            busy=[BusySlot(start=conflict.start, end=conflict.end)],
            events=[conflict],
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        assert len(out["side_effect"].conflicts) == 1
        assert out["side_effect"].conflicts[0].title == "週會"

    def test_ical_uid_collision_returns_duplicate(self, fake_email):
        calendar_client = _mock_calendar_client(
            insert_result=iCalUidExists("dup"),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarDuplicate)

    def test_scope_missing_on_insert_degrades(self, fake_email, caplog):
        import logging as _logging

        calendar_client = _mock_calendar_client(
            insert_result=CalendarScopeNotGranted("403"),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        with caplog.at_level(_logging.WARNING):
            out = sg._create_node({
                "email": fake_email, "analysis": None,
                "side_effect": None, "extracted": _draft(),
            })
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "scope_missing"
        # WARNING logged exactly once for the pod
        assert any("scope not granted" in m for m in caplog.messages)

    def test_scope_missing_on_freebusy_degrades(self, fake_email):
        calendar_client = _mock_calendar_client(
            freebusy_error=CalendarScopeNotGranted("403"),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarSkipped)
        # Insert MUST NOT have been called (skip early on freebusy 403)
        calendar_client.insert_event.assert_not_called()

    def test_freebusy_generic_error_still_inserts(self, fake_email):
        # Non-scope error on freebusy → warn but still try insert
        # (conflict summary just missing).
        calendar_client = _mock_calendar_client(
            freebusy_error=CalendarError("500"),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        assert out["side_effect"].conflicts == []
        calendar_client.insert_event.assert_called_once()

    def test_skip_when_extracted_none(self, fake_email):
        sg = _sg(_build_model_returning(None), MagicMock(), _mock_calendar_client())
        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert isinstance(out["side_effect"], CalendarSkipped)

    def test_passes_through_existing_side_effect(self, fake_email):
        pre_skip = CalendarSkipped(reason="extraction_failed", detail="test")
        calendar_client = _mock_calendar_client()
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": pre_skip, "extracted": None,
        })
        # Empty dict = pass-through, no new side_effect written
        assert out == {}
        calendar_client.check_free_busy.assert_not_called()


# ---------- payload composition -----------------------------------------


class TestInsertPayload:
    def test_conflict_summary_in_description(self, fake_email):
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload
        from zashiki_warasi.core.schemas import CalendarConflict

        conflicts = [
            CalendarConflict(
                title="週會",
                start=datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
                end=datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc),
            )
        ]
        payload = _build_insert_payload(_draft(), "msg-1", "uid-1", conflicts)

        assert payload["status"] == "tentative"
        assert payload["iCalUID"] == "uid-1"
        assert "Time conflicts" in payload["description"]
        assert "週會" in payload["description"]
        assert payload["extendedProperties"]["private"]["zashiki_message_id"] == "msg-1"

    def test_no_conflicts_no_conflict_section(self, fake_email):
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        payload = _build_insert_payload(_draft(), "msg-2", "uid-2", [])
        assert "Time conflicts" not in payload["description"]

    def test_more_than_3_conflicts_truncated(self, fake_email):
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload
        from zashiki_warasi.core.schemas import CalendarConflict

        conflicts = [
            CalendarConflict(
                title=f"event{i}",
                start=datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
                end=datetime(2026, 9, 15, 14, 30, tzinfo=timezone.utc),
            )
            for i in range(5)
        ]
        payload = _build_insert_payload(_draft(), "msg-3", "uid-3", conflicts)
        assert "and 2 more" in payload["description"]
