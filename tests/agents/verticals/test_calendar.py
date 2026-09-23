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
        body_plain="See attached invite for 2026-09-15 14:00.",
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

    def test_llm_stringified_null_title_skips_event(self, fake_email):
        """LLM emits `"title": "null"` (str, not JSON null); Pydantic
        accepts. Without coercion the payload's `summary` becomes
        literal 'null' and Google either 400s or creates a trash
        event named 'null'. Sanitizer must treat null-ish strings
        as missing and skip."""
        bad_draft = CalendarEventDraft(
            title="null",
            start=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc),
            location="null",
            ical_uid="null",
            timezone_hint="null",
        )
        model = _build_model_returning(bad_draft)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is None
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "extraction_failed"
        assert "null-ish title" in out["side_effect"].detail

    def test_llm_null_optional_fields_coerced_to_none(self, fake_email):
        """title populated but optional fields are literal `"null"` —
        those should be coerced to real None so downstream payload
        composition sees clean data."""
        draft = CalendarEventDraft(
            title="Real Talk",
            start=datetime(2026, 9, 29, 12, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 29, 13, 0, tzinfo=timezone.utc),
            location="null",
            description="None",
            ical_uid="undefined",
            timezone_hint="null",
        )
        model = _build_model_returning(draft)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        extracted = out["extracted"]
        assert extracted is not None
        assert extracted.title == "Real Talk"
        assert extracted.location is None
        assert extracted.description is None
        assert extracted.ical_uid is None
        assert extracted.timezone_hint is None

    def test_llm_utc_aware_draft_forced_to_naive(self, fake_email):
        """LLM emits `Z` on a wall-clock Taipei time → Pydantic makes
        it UTC-aware → our step MUST strip tzinfo so the payload
        builder treats it as wall-clock in the sanitized hint tz."""
        utc_aware_draft = CalendarEventDraft(
            title="GDG",
            start=datetime(2026, 9, 17, 19, 30, tzinfo=timezone.utc),
            end=datetime(2026, 9, 17, 21, 30, tzinfo=timezone.utc),
        )
        model = _build_model_returning(utc_aware_draft)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({"email": fake_email, "analysis": None, "side_effect": None, "extracted": None})
        extracted = out["extracted"]
        assert extracted is not None
        assert extracted.start.tzinfo is None
        assert extracted.end.tzinfo is None
        assert extracted.start == datetime(2026, 9, 17, 19, 30)
        assert extracted.end == datetime(2026, 9, 17, 21, 30)

    def test_ics_path_datetime_stays_tz_aware(self, fake_email_with_ics):
        """`.ics` path returns tz-aware datetimes with genuine
        VTIMEZONE data — MUST NOT be stripped; the payload builder
        will `astimezone` them to the hint tz for cross-zone cases."""
        # A minimal .ics with an explicit UTC time — the ics parser
        # will yield a UTC-aware datetime (real VTIMEZONE / trailing Z).
        ics = (
            b"BEGIN:VCALENDAR\r\n"
            b"VERSION:2.0\r\n"
            b"BEGIN:VEVENT\r\n"
            b"UID:real-utc@example.com\r\n"
            b"SUMMARY:Cross-zone Event\r\n"
            b"DTSTART:20260917T113000Z\r\n"
            b"DTEND:20260917T133000Z\r\n"
            b"END:VEVENT\r\n"
            b"END:VCALENDAR\r\n"
        )
        model = _build_model_returning(None)
        sg = _sg(model, _mock_gmail_client_returning_ics(ics), _mock_calendar_client())

        out = sg._extract_node({"email": fake_email_with_ics, "analysis": None, "side_effect": None, "extracted": None})
        extracted = out["extracted"]
        assert extracted is not None
        # `.ics` path preserves tz-aware — the payload builder handles
        # the cross-zone astimezone at insert time.
        assert extracted.start.tzinfo is not None


# ---------- _extract_node short-circuit + LLM error surfacing -----------


class TestExtractShortCircuit:
    """§8.6 / v1.4.1: pre-LLM guard when body has no date/time signal.

    Second line of defense behind the classifier's `講座資訊` boundary —
    catches residual noise (Coursera promos, MOOC recommendations)
    without paying for an LLM call.
    """

    def _coursera_email(self) -> EmailMessage:
        return EmailMessage(
            id="msg-coursera",
            thread_id="t",
            history_id=101,
            from_address="Coursera@m.learn.coursera.org",
            subject="Recommended: Build Batch Data Pipelines on Google Cloud",
            body_plain=(
                "Explore courses recommended for you. Dataflow, Kafka, "
                "Databricks and IBM ETL fundamentals — all self-paced. "
                "Enroll anytime; no cohort dates."
            ),
            received_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
            attachments=[],
        )

    def _email_with(self, body: str, subject: str = "邀請") -> EmailMessage:
        return EmailMessage(
            id="msg-x",
            thread_id="t",
            history_id=102,
            from_address="x@example.com",
            subject=subject,
            body_plain=body,
            received_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
            attachments=[],
        )

    def test_no_date_signal_short_circuits_llm_not_called(self):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({
            "email": self._coursera_email(),
            "analysis": None,
            "side_effect": None,
            "extracted": None,
        })
        assert out["extracted"] is None
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "no_event_signal"
        model.with_structured_output.return_value.invoke.assert_not_called()

    def test_numeric_date_9_17_passes_through_to_llm(self):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        email = self._email_with("邀請你來活動,9/17 19:30 見。")
        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is not None
        model.with_structured_output.return_value.invoke.assert_called_once()

    def test_english_weekday_time_passes_through_to_llm(self):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        email = self._email_with("Talk on Friday 2:00pm at HQ")
        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is not None
        model.with_structured_output.return_value.invoke.assert_called_once()

    def test_chinese_weekday_passes_through_to_llm(self):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        email = self._email_with("週三下午 3 點在會議室 A 見。")
        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is not None
        model.with_structured_output.return_value.invoke.assert_called_once()

    def test_ics_bypasses_short_circuit(self, fake_email_with_ics):
        """.ics attachment always wins — no short-circuit consideration."""
        ics_bytes = (
            b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
            b"UID:ics-real@example.com\r\n"
            b"DTSTART;TZID=Asia/Taipei:20260915T140000\r\n"
            b"DTEND;TZID=Asia/Taipei:20260915T150000\r\n"
            b"SUMMARY:Real Meeting\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        # Override body_plain to have NO date signal — ics should still win.
        email = fake_email_with_ics.model_copy(
            update={"body_plain": "See attached."}
        )
        model = _build_model_returning(None)
        sg = _sg(model, _mock_gmail_client_returning_ics(ics_bytes), _mock_calendar_client())

        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is not None
        assert out["extracted"].title == "Real Meeting"
        model.with_structured_output.return_value.invoke.assert_not_called()


class TestExtractLLMErrorSurface:
    """§8.6 / v1.4.1: LLM extraction failure logs carry `str(exc)`,
    not just the class name — makes BadRequestError diagnosable from
    pod logs without re-running.
    """

    def _email_with_date(self) -> EmailMessage:
        return EmailMessage(
            id="msg-llmerr",
            thread_id="t",
            history_id=103,
            from_address="x@example.com",
            subject="邀請",
            body_plain="2026-09-17 19:30 在會場 A。",
            received_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
            attachments=[],
        )

    def test_llm_exception_body_lands_in_log_and_detail(self, caplog):
        import logging as _logging

        class FakeBadRequest(Exception):
            pass

        model = MagicMock(name="chat_model")
        structured = MagicMock(name="structured")
        structured.invoke.side_effect = FakeBadRequest(
            "context length exceeded: 8192 > 4096 tokens"
        )
        model.with_structured_output.return_value = structured
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        with caplog.at_level(_logging.WARNING):
            out = sg._extract_node({
                "email": self._email_with_date(),
                "analysis": None, "side_effect": None, "extracted": None,
            })

        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "extraction_failed"
        # Class name AND body BOTH present in detail
        assert "FakeBadRequest" in out["side_effect"].detail
        assert "context length exceeded" in out["side_effect"].detail
        # WARNING log carries the same
        warn = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any(
            "FakeBadRequest" in r.message and "context length" in r.message
            for r in warn
        )

    def test_pydantic_validation_error_summarized_cleanly(self):
        """Real-world Bio-protocol shakedown surfaced a Pydantic
        ValidationError with URL + [type=...] + newlines dumped into
        Telegram. Summarizer must give a compact `loc: msg (and N more)`
        form, no Pydantic doc URL, no whitespace churn."""
        from pydantic import BaseModel
        from pydantic import ValidationError

        from zashiki_warasi.agents.verticals.calendar import (
            _summarize_exception,
        )

        class _M(BaseModel):
            start: datetime
            end: datetime

        try:
            _M(start="0000-01-01T00:00:00-00:00", end="0000-01-01T00:00:00-00:00")
        except ValidationError as exc:
            summary = _summarize_exception(exc)

        assert "ValidationError:" in summary
        assert "start:" in summary
        assert "year 0" in summary
        assert "and 1 more" in summary  # 2 errors → and 1 more
        # Cleanup expectations
        assert "https://" not in summary
        assert "\n" not in summary
        assert "For further information" not in summary
        assert len(summary) < 260

    def test_llm_exception_body_truncated_at_512(self):
        class FakeExc(Exception):
            pass

        long_msg = "x" * 1000  # 1000 > 512
        model = MagicMock(name="chat_model")
        structured = MagicMock(name="structured")
        structured.invoke.side_effect = FakeExc(long_msg)
        model.with_structured_output.return_value = structured
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({
            "email": self._email_with_date(),
            "analysis": None, "side_effect": None, "extracted": None,
        })
        detail = out["side_effect"].detail
        assert detail.endswith("…")
        # Class-name prefix + truncated body + ellipsis marker; length
        # bounded (well under original 1000).
        assert len(detail) < 600
        assert "FakeExc" in detail


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

    def test_naive_draft_gets_aware_before_freebusy(self, fake_email):
        """LLM-path 8.7 fix produces naive datetimes; `_create_node`
        MUST attach the effective tz before calling freebusy, else
        `GoogleCalendarClient._iso` raises ValueError. Regression
        seen live at commit f8931397."""
        naive_draft = CalendarEventDraft(
            title="test",
            start=datetime(2026, 9, 17, 19, 30),  # naive
            end=datetime(2026, 9, 17, 21, 30),
        )
        calendar_client = _mock_calendar_client(
            busy=[],
            insert_result=InsertedEvent(
                id="gcal-new",
                ical_uid="uid",
                view_url="https://calendar.google.com/xxx",
            ),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": naive_draft,
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        # freebusy was called with tz-aware arguments — extract them
        args = calendar_client.check_free_busy.call_args.args
        assert args[0].tzinfo is not None
        assert args[1].tzinfo is not None

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

    def test_tz_aware_utc_draft_rebased_to_default_taipei(self):
        """UTC-aware draft times get astimezoned to Asia/Taipei
        (default) and emitted naive so Google honors `timeZone`
        rather than the offset — prevents LLM stray-Z bug."""
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        draft = CalendarEventDraft(
            title="test",
            start=datetime(2026, 9, 17, 11, 30, tzinfo=timezone.utc),
            end=datetime(2026, 9, 17, 13, 30, tzinfo=timezone.utc),
        )
        payload = _build_insert_payload(draft, "m", "u", [])
        # 11:30 UTC = 19:30 Asia/Taipei; emitted naive; timeZone hint set.
        assert payload["start"]["dateTime"] == "2026-09-17T19:30:00"
        assert payload["start"]["timeZone"] == "Asia/Taipei"
        assert payload["end"]["dateTime"] == "2026-09-17T21:30:00"
        assert payload["end"]["timeZone"] == "Asia/Taipei"
        # dateTime must NOT carry an offset — Google would prefer it
        # over `timeZone` and mis-place the event.
        assert "+" not in payload["start"]["dateTime"]
        assert "Z" not in payload["start"]["dateTime"]

    def test_naive_draft_left_naive_with_timezone_hint(self):
        """Naive draft (LLM prompt path when the LLM follows instructions)
        keeps its wall-clock time; the hint's TZ is what Google places
        it in."""
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        draft = CalendarEventDraft(
            title="test",
            start=datetime(2026, 9, 17, 19, 30),  # naive
            end=datetime(2026, 9, 17, 21, 30),
            timezone_hint="Asia/Taipei",
        )
        payload = _build_insert_payload(draft, "m", "u", [])
        assert payload["start"]["dateTime"] == "2026-09-17T19:30:00"
        assert payload["start"]["timeZone"] == "Asia/Taipei"

    def test_timezone_hint_overrides_default(self):
        """When the .ics parser recovered a non-default timezone (e.g.
        Asia/Tokyo), it lands in the payload's `timeZone` field."""
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        draft = CalendarEventDraft(
            title="test",
            start=datetime(2026, 9, 17, 19, 30),
            end=datetime(2026, 9, 17, 21, 30),
            timezone_hint="Asia/Tokyo",
        )
        payload = _build_insert_payload(draft, "m", "u", [])
        assert payload["start"]["timeZone"] == "Asia/Tokyo"
        assert payload["start"]["dateTime"] == "2026-09-17T19:30:00"


class TestSanitizeTzHint:
    @pytest.mark.parametrize(
        "bad_hint",
        ["UTC", "utc", "Etc/UTC", "etc/utc", "GMT", "gmt", "Z", "z", ""],
    )
    def test_utc_like_hints_fall_back_to_default(self, bad_hint, caplog):
        """LLM mislabeling a Taipei-local time as UTC would place the
        event at 03:30/04:30 next day. Sanitizer intercepts."""
        import logging

        from zashiki_warasi.agents.verticals.calendar import _sanitize_tz_hint

        with caplog.at_level(logging.WARNING):
            out = _sanitize_tz_hint(bad_hint, default="Asia/Taipei")

        assert out == "Asia/Taipei"
        # Non-empty bad values also trigger a WARN log carrying the
        # discarded value; empty string is silent (no LLM output).
        if bad_hint:
            assert any(
                "rejecting untrusted timezone_hint" in rec.message
                and repr(bad_hint) in rec.message
                for rec in caplog.records
            )

    def test_real_zones_pass_through(self):
        from zashiki_warasi.agents.verticals.calendar import _sanitize_tz_hint

        assert _sanitize_tz_hint("Asia/Tokyo", default="Asia/Taipei") == "Asia/Tokyo"
        assert _sanitize_tz_hint("Asia/Taipei", default="Asia/Taipei") == "Asia/Taipei"
        assert _sanitize_tz_hint("America/New_York", default="Asia/Taipei") == "America/New_York"

    def test_none_falls_back_silently(self, caplog):
        import logging

        from zashiki_warasi.agents.verticals.calendar import _sanitize_tz_hint

        with caplog.at_level(logging.WARNING):
            out = _sanitize_tz_hint(None, default="Asia/Taipei")

        assert out == "Asia/Taipei"
        # None is the normal "LLM omitted the field" case — no warn.
        assert not any(
            "rejecting untrusted" in rec.message for rec in caplog.records
        )

    @pytest.mark.parametrize("bad_hint", ["null", "None", "NIL", "undefined", ""])
    def test_llm_stringified_null_hints_fall_back(self, bad_hint, caplog):
        """LLM emits literal `"null"` (str, not JSON null); Pydantic
        accepts as str; without validation Google 400s with 'Invalid
        time zone definition for start time.' Observed on the
        Bio-protocol T-cell webinar smoke."""
        import logging as _logging

        from zashiki_warasi.agents.verticals.calendar import _sanitize_tz_hint

        with caplog.at_level(_logging.WARNING):
            out = _sanitize_tz_hint(bad_hint, default="Asia/Taipei")
        assert out == "Asia/Taipei"

    def test_unresolvable_zoneinfo_falls_back(self):
        """A syntactically-valid but non-existent zone (`Middle-earth/Shire`)
        must fall back — otherwise Google 400s on the payload."""
        from zashiki_warasi.agents.verticals.calendar import _sanitize_tz_hint

        assert (
            _sanitize_tz_hint("Middle-earth/Shire", default="Asia/Taipei")
            == "Asia/Taipei"
        )

    def test_end_to_end_llm_utc_hint_rejected_in_payload(self):
        """The bug we're fixing: LLM sets timezone_hint='UTC' + naive
        start='19:30'. Sanitizer overrides so Google places it in
        Asia/Taipei, not UTC."""
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        draft = CalendarEventDraft(
            title="GDG",
            start=datetime(2026, 9, 17, 19, 30),
            end=datetime(2026, 9, 17, 21, 30),
            timezone_hint="UTC",
        )
        payload = _build_insert_payload(draft, "m", "u", [])
        assert payload["start"]["timeZone"] == "Asia/Taipei"
        assert payload["start"]["dateTime"] == "2026-09-17T19:30:00"
        assert payload["end"]["timeZone"] == "Asia/Taipei"
