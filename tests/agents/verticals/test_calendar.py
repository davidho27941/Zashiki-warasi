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
    ical_uid_hits: list[ExistingEvent] | Exception | None = None,
    fingerprint_hits: list[ExistingEvent] | Exception | None = None,
) -> MagicMock:
    client = MagicMock(name="calendar_client")
    if freebusy_error is not None:
        client.check_free_busy.side_effect = freebusy_error
    else:
        client.check_free_busy.return_value = busy or []
    client.list_events_in_window.return_value = events or []
    # v1.7.0 dedup lookups — default to MISS ([]), else every create
    # test would see MagicMock's truthy auto-attribute as a "hit".
    if isinstance(ical_uid_hits, Exception):
        client.list_events_by_ical_uid.side_effect = ical_uid_hits
    else:
        client.list_events_by_ical_uid.return_value = ical_uid_hits or []
    if isinstance(fingerprint_hits, Exception):
        client.list_events_by_private_extended_property.side_effect = (
            fingerprint_hits
        )
    else:
        client.list_events_by_private_extended_property.return_value = (
            fingerprint_hits or []
        )
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
        payload = _build_insert_payload(_draft(), "msg-1", "uid-1", conflicts, "fp-test")

        assert payload["status"] == "tentative"
        assert payload["iCalUID"] == "uid-1"
        assert "Time conflicts" in payload["description"]
        assert "週會" in payload["description"]
        assert payload["extendedProperties"]["private"]["zashiki_message_id"] == "msg-1"

    def test_no_conflicts_no_conflict_section(self, fake_email):
        from zashiki_warasi.agents.verticals.calendar import _build_insert_payload

        payload = _build_insert_payload(_draft(), "msg-2", "uid-2", [], "fp-test")
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
        payload = _build_insert_payload(_draft(), "msg-3", "uid-3", conflicts, "fp-test")
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
        payload = _build_insert_payload(draft, "m", "u", [], "fp-test")
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
        payload = _build_insert_payload(draft, "m", "u", [], "fp-test")
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
        payload = _build_insert_payload(draft, "m", "u", [], "fp-test")
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
        payload = _build_insert_payload(draft, "m", "u", [], "fp-test")
        assert payload["start"]["timeZone"] == "Asia/Taipei"
        assert payload["start"]["dateTime"] == "2026-09-17T19:30:00"
        assert payload["end"]["timeZone"] == "Asia/Taipei"


def _read_graph_count(node: str, vertical: str, outcome: str) -> float:
    """Read `zashiki_graph_node_duration_seconds_count` for one label
    combination. Labels render alphabetically in the exposition format
    (`node`, `outcome`, `vertical`) regardless of declaration order."""
    from prometheus_client import generate_latest

    from zashiki_warasi.observability import REGISTRY

    family = "zashiki_graph_node_duration_seconds_count"
    selector = (
        f'node="{node}",outcome="{outcome}",vertical="{vertical}"'
    )
    total = 0.0
    for line in generate_latest(REGISTRY).decode().splitlines():
        if line.startswith(family) and selector in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


class TestExtractGraphSpanOutcomes:
    """§ 7 (v1.5.0): pin the outcome-label branches at `_extract_node`.

    Each real return path (§ 2's outcome mapping) must land in the
    expected `zashiki_graph_node_duration_seconds{outcome=…}` bucket
    so future dashboard queries `sum by (outcome)` stay meaningful.
    Regression guard against someone removing a `gs.outcome = …`
    line during a refactor."""

    def test_success_outcome_on_happy_extract(self, fake_email):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        before = _read_graph_count("extract", "calendar_sg", "success")
        sg._extract_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        after = _read_graph_count("extract", "calendar_sg", "success")
        assert after == before + 1.0

    def test_skipped_outcome_on_no_event_signal_short_circuit(self):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())
        coursera_email = EmailMessage(
            id="msg-cs",
            thread_id="t",
            history_id=200,
            from_address="promo@example.com",
            subject="Recommended: MOOC",
            body_plain=(
                "Enroll anytime; explore courses recommended for you."
            ),
            received_at=datetime(2026, 9, 14, 10, 0, tzinfo=timezone.utc),
            attachments=[],
        )

        before = _read_graph_count("extract", "calendar_sg", "skipped")
        sg._extract_node({
            "email": coursera_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        after = _read_graph_count("extract", "calendar_sg", "skipped")
        assert after == before + 1.0

    def test_error_outcome_on_llm_exception(self, fake_email):
        class LLMBoom(RuntimeError):
            pass

        model = MagicMock(name="chat_model")
        structured = MagicMock(name="structured")
        structured.invoke.side_effect = LLMBoom("upstream 500")
        model.with_structured_output.return_value = structured
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        before = _read_graph_count("extract", "calendar_sg", "error")
        sg._extract_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        after = _read_graph_count("extract", "calendar_sg", "error")
        assert after == before + 1.0


class TestCreateGraphSpanOutcomes:
    def test_success_outcome_on_clean_insert(self, fake_email):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())
        draft = _draft()

        before = _read_graph_count("create", "calendar_sg", "success")
        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        after = _read_graph_count("create", "calendar_sg", "success")
        assert after == before + 1.0

    def test_skipped_outcome_when_upstream_side_effect_set(self, fake_email):
        model = _build_model_returning(_draft())
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        before = _read_graph_count("create", "calendar_sg", "skipped")
        # upstream extract returned a CalendarSkipped — create should
        # no-op and record outcome=skipped.
        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": CalendarSkipped(
                reason="no_event_signal", detail="nothing to create"
            ),
            "extracted": None,
        })
        after = _read_graph_count("create", "calendar_sg", "skipped")
        assert after == before + 1.0

    def test_skipped_outcome_on_ical_uid_collision(self, fake_email):
        model = _build_model_returning(_draft())
        cal_client = _mock_calendar_client(
            insert_result=iCalUidExists("dup")
        )
        sg = _sg(model, MagicMock(), cal_client)
        draft = _draft()

        before = _read_graph_count("create", "calendar_sg", "skipped")
        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        after = _read_graph_count("create", "calendar_sg", "skipped")
        assert after == before + 1.0

    def test_error_outcome_on_insert_failure(self, fake_email):
        model = _build_model_returning(_draft())
        cal_client = _mock_calendar_client(
            insert_result=CalendarError("HTTP 500 upstream")
        )
        sg = _sg(model, MagicMock(), cal_client)
        draft = _draft()

        before = _read_graph_count("create", "calendar_sg", "error")
        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        after = _read_graph_count("create", "calendar_sg", "error")
        assert after == before + 1.0

    def test_error_outcome_on_scope_not_granted(self, fake_email):
        model = _build_model_returning(_draft())
        cal_client = _mock_calendar_client(
            insert_result=CalendarScopeNotGranted("403 missing scope")
        )
        sg = _sg(model, MagicMock(), cal_client)
        draft = _draft()

        before = _read_graph_count("create", "calendar_sg", "error")
        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        after = _read_graph_count("create", "calendar_sg", "error")
        assert after == before + 1.0


# ---------- v1.7.0 dedup -------------------------------------------------


class TestNormalization:
    def test_summary_strips_noise_prefixes_iteratively(self):
        from zashiki_warasi.agents.verticals.calendar import _normalize_summary

        assert (
            _normalize_summary("Fwd: Fwd: [Reminder] Bio-protocol Webinar")
            == "bio-protocol webinar"
        )
        assert _normalize_summary("Re: [提醒] 產品週會") == "產品週會"
        assert (
            _normalize_summary("[Starting Soon]  Webinar")
            == "webinar"
        )
        assert _normalize_summary("[即將開始] 講座") == "講座"

    def test_summary_collapses_whitespace_and_lowercases(self):
        from zashiki_warasi.agents.verticals.calendar import _normalize_summary

        assert _normalize_summary("  Product\t\tWeekly   SYNC ") == (
            "product weekly sync"
        )

    def test_prefix_inside_title_survives(self):
        from zashiki_warasi.agents.verticals.calendar import _normalize_summary

        # Only LEADING noise strips — a legit "re:" mid-title stays.
        assert _normalize_summary("Deep dive: Re: invent recap") == (
            "deep dive: re: invent recap"
        )

    def test_bucket_rounds_down(self):
        from zoneinfo import ZoneInfo

        from zashiki_warasi.agents.verticals.calendar import _bucket_start_5min

        utc = ZoneInfo("UTC")
        assert _bucket_start_5min(datetime(2026, 9, 15, 14, 7), utc) == (
            "2026-09-15T14:05"
        )
        assert _bucket_start_5min(datetime(2026, 9, 15, 14, 0), utc) == (
            "2026-09-15T14:00"
        )
        assert _bucket_start_5min(
            datetime(2026, 9, 15, 14, 59, 59), utc
        ) == "2026-09-15T14:55"

    def test_bucket_converts_aware_and_naive_to_same_utc(self):
        """The .ics path yields tz-aware starts; the LLM path yields
        naive + hint. Same instant MUST land in the same bucket."""
        from zoneinfo import ZoneInfo

        from zashiki_warasi.agents.verticals.calendar import _bucket_start_5min

        taipei = ZoneInfo("Asia/Taipei")
        aware = datetime(2026, 9, 15, 6, 2, tzinfo=timezone.utc)
        naive_taipei = datetime(2026, 9, 15, 14, 3)  # 06:03 UTC
        assert _bucket_start_5min(aware, taipei) == _bucket_start_5min(
            naive_taipei, taipei
        )

    def test_ics_attachment_detection_mime_variants(self):
        """Gmail re-types a forwarded .ics to application/ics; anything
        else with a .ics filename is caught by the fallback."""
        from zashiki_warasi.agents.verticals.calendar import (
            _find_ics_attachment,
        )
        from zashiki_warasi.core.schemas import AttachmentMeta

        def _email_with(mime, filename="invite.ics"):
            return EmailMessage(
                id="m", thread_id="t", history_id=1,
                from_address="a@x", subject="s",
                received_at=datetime(2026, 9, 27, tzinfo=timezone.utc),
                attachments=[AttachmentMeta(
                    attachment_id="att-1", filename=filename,
                    mime_type=mime, size=100,
                )],
            )

        assert _find_ics_attachment(_email_with("text/calendar")) is not None
        assert _find_ics_attachment(
            _email_with('text/calendar; charset="US-ASCII"')
        ) is not None
        assert _find_ics_attachment(_email_with("application/ics")) is not None
        assert _find_ics_attachment(
            _email_with("application/octet-stream", "event.ICS")
        ) is not None
        assert _find_ics_attachment(
            _email_with("application/pdf", "notes.pdf")
        ) is None


class TestComputeFingerprint:
    def _fp(self, **overrides) -> str:
        from zashiki_warasi.agents.verticals.calendar import (
            _compute_fingerprint,
        )

        base = dict(
            title="Bio-protocol Webinar",
            start=datetime(2026, 9, 29, 9, 0),
            end=datetime(2026, 9, 29, 10, 0),
            location="Zoom",
            timezone_hint="Asia/Taipei",
        )
        base.update(overrides)
        return _compute_fingerprint(
            CalendarEventDraft(**base), default_tz="Asia/Taipei"
        )

    def test_deterministic(self):
        assert self._fp() == self._fp()

    def test_reminder_variants_collapse(self):
        assert self._fp(title="[Reminder] Bio-protocol   webinar") == self._fp()
        assert self._fp(title="Fwd: Re: BIO-PROTOCOL Webinar") == self._fp()

    def test_start_jitter_within_bucket_collapses(self):
        assert self._fp(start=datetime(2026, 9, 29, 9, 4)) == self._fp()

    def test_different_bucket_differs(self):
        assert self._fp(start=datetime(2026, 9, 29, 9, 5)) != self._fp()

    def test_location_not_in_fingerprint(self):
        """V2 dropped location: the .ics path carries LOCATION but the
        LLM body path often has none — including it broke Layer 2 on
        mixed invite+reminder series (Round B smoke, 2026-09-27)."""
        assert self._fp(location="Google Meet") == self._fp(location=None)
        assert self._fp(location="信義區辦公室") == self._fp()

    def test_end_not_in_fingerprint(self):
        assert self._fp(end=datetime(2026, 9, 29, 11, 30)) == self._fp()


def _existing_hit() -> ExistingEvent:
    return ExistingEvent(
        id="gcal-existing",
        title="產品週會",
        start=datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
        end=datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
    )


class TestDedupLayerOne:
    def test_ical_uid_hit_skips_without_insert(self, fake_email):
        calendar_client = _mock_calendar_client(
            ical_uid_hits=[_existing_hit()],
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "duplicate_by_ical_uid"
        assert "gcal-existing" in out["side_effect"].detail
        calendar_client.insert_event.assert_not_called()

    def test_ical_uid_miss_falls_through_to_layer_two(self, fake_email):
        calendar_client = _mock_calendar_client()
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        calendar_client.list_events_by_ical_uid.assert_called_once_with(
            "ics-uid@example.com"
        )
        calendar_client.list_events_by_private_extended_property.assert_called_once()

    def test_no_ical_uid_skips_layer_one(self, fake_email):
        draft = CalendarEventDraft(
            title="test",
            start=datetime(2026, 9, 17, 19, 30),
            end=datetime(2026, 9, 17, 21, 30),
        )
        calendar_client = _mock_calendar_client()
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        calendar_client.list_events_by_ical_uid.assert_not_called()

    def test_layer_one_error_falls_through_to_layer_two(
        self, fake_email, caplog
    ):
        import logging as _logging

        calendar_client = _mock_calendar_client(
            ical_uid_hits=CalendarError("500"),
            fingerprint_hits=[_existing_hit()],
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        with caplog.at_level(_logging.WARNING):
            out = sg._create_node({
                "email": fake_email, "analysis": None,
                "side_effect": None, "extracted": _draft(),
            })
        assert out["side_effect"].reason == "duplicate_by_fingerprint"
        assert any("Layer 1" in m for m in caplog.messages)


class TestDedupLayerTwo:
    def test_fingerprint_hit_skips_without_insert(self, fake_email):
        draft = CalendarEventDraft(
            title="[Reminder] Bio-protocol Webinar",
            start=datetime(2026, 9, 29, 9, 0),
            end=datetime(2026, 9, 29, 10, 0),
        )
        calendar_client = _mock_calendar_client(
            fingerprint_hits=[_existing_hit()],
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": draft,
        })
        assert isinstance(out["side_effect"], CalendarSkipped)
        assert out["side_effect"].reason == "duplicate_by_fingerprint"
        calendar_client.insert_event.assert_not_called()

    def test_fingerprint_lookup_uses_versioned_key_and_window(
        self, fake_email
    ):
        calendar_client = _mock_calendar_client()
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        call = calendar_client.list_events_by_private_extended_property.call_args
        assert call.args[0] == "zwFingerprintV2"
        assert len(call.args[1]) == 40  # sha1 hex
        window = call.kwargs["time_max"] - call.kwargs["time_min"]
        assert window.days == 2  # start ± 1d

    def test_miss_inserts_with_fingerprint_stamped(self, fake_email):
        calendar_client = _mock_calendar_client()
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        out = sg._create_node({
            "email": fake_email, "analysis": None,
            "side_effect": None, "extracted": _draft(),
        })
        assert isinstance(out["side_effect"], CalendarCreated)
        payload = calendar_client.insert_event.call_args.args[0]
        stamped = payload["extendedProperties"]["private"]["zwFingerprintV2"]
        # The stamp must be the SAME hash the lookup queried —
        # single-source `_compute_fingerprint` (design D3 property).
        queried = (
            calendar_client.list_events_by_private_extended_property
            .call_args.args[1]
        )
        assert stamped == queried
        # Forensic message-id key still present alongside.
        assert (
            payload["extendedProperties"]["private"]["zashiki_message_id"]
            == fake_email.id
        )

    def test_reminder_variant_hits_same_fingerprint_as_invite(
        self, fake_email
    ):
        from zashiki_warasi.agents.verticals.calendar import (
            _compute_fingerprint,
        )

        invite = CalendarEventDraft(
            title="Bio-protocol Webinar",
            start=datetime(2026, 9, 29, 9, 0),
            end=datetime(2026, 9, 29, 10, 0),
        )
        reminder = CalendarEventDraft(
            title="[提醒] Fwd: Bio-protocol  Webinar",
            start=datetime(2026, 9, 29, 9, 3),  # LLM jitter within bucket
            end=datetime(2026, 9, 29, 10, 30),  # end differs — excluded
        )
        assert _compute_fingerprint(
            invite, default_tz="Asia/Taipei"
        ) == _compute_fingerprint(reminder, default_tz="Asia/Taipei")


class TestDedupBothLayersFail:
    def test_both_lookups_error_insert_proceeds(self, fake_email, caplog):
        import logging as _logging

        calendar_client = _mock_calendar_client(
            ical_uid_hits=CalendarError("500"),
            fingerprint_hits=CalendarError("503"),
        )
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        with caplog.at_level(_logging.WARNING):
            out = sg._create_node({
                "email": fake_email, "analysis": None,
                "side_effect": None, "extracted": _draft(),
            })
        # Best-effort semantics: dedup failure never blocks the event.
        assert isinstance(out["side_effect"], CalendarCreated)
        warned = [m for m in caplog.messages if "dedup Layer" in m]
        assert len(warned) == 2
        calendar_client.insert_event.assert_called_once()


class TestDuplicateSkipNotifyWording:
    @pytest.mark.parametrize(
        "reason", ["duplicate_by_ical_uid", "duplicate_by_fingerprint"]
    )
    def test_duplicate_reasons_render_friendly_line(self, reason):
        from zashiki_warasi.agents.email_agent import (
            _format_calendar_skipped,
        )

        text = _format_calendar_skipped(
            CalendarSkipped(
                reason=reason,
                detail="產品週會 @ 2026-09-15T14:00:00+00:00 (event abc)",
            )
        )
        assert "已跳過" in text
        assert "此事件已存在於行事曆" in text
        assert "產品週會" in text
        # Distinct from the failure wordings.
        assert "行事曆事件未建立" not in text

    def test_failure_reasons_unchanged(self):
        from zashiki_warasi.agents.email_agent import (
            _format_calendar_skipped,
        )

        text = _format_calendar_skipped(
            CalendarSkipped(reason="no_event_signal")
        )
        assert "行事曆事件未建立" in text
        assert "內文無明確活動時間" in text
        assert "已存在" not in text


# ---------- v1.7.0 inline .ics extraction (design D7) --------------------


_INLINE_VCAL = """BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
METHOD:REQUEST
BEGIN:VEVENT
DTSTART:20260930T060000Z
DTEND:20260930T070000Z
DTSTAMP:20260927T000000Z
UID:inline-real-uid@google.com
SUMMARY:Test Event
END:VEVENT
END:VCALENDAR
"""


class TestExtractInlineIcs:
    def _email_with_inline_ics(self) -> EmailMessage:
        return EmailMessage(
            id="msg-inline-ics",
            thread_id="t",
            history_id=100,
            from_address="calendar-notification@google.com",
            subject="邀請:Test Event",
            body_plain="You have been invited.",
            received_at=datetime(2026, 9, 27, 10, 0, tzinfo=timezone.utc),
            attachments=[],
            ics_inline=_INLINE_VCAL,
        )

    def test_inline_ics_extracts_without_llm(self):
        model = _build_model_returning(None)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        out = sg._extract_node({
            "email": self._email_with_inline_ics(), "analysis": None,
            "side_effect": None, "extracted": None,
        })
        draft = out["extracted"]
        assert draft is not None
        assert draft.ical_uid == "inline-real-uid@google.com"
        assert draft.title == "Test Event"
        # Deterministic path — the LLM must NOT have been invoked.
        model.with_structured_output.return_value.invoke.assert_not_called()

    def test_attachment_ics_takes_priority_over_inline(
        self, fake_email_with_ics
    ):
        """When a REAL .ics attachment exists, it wins; ics_inline is
        only the fallback for invite-style inline parts."""
        email = fake_email_with_ics.model_copy(
            update={"ics_inline": _INLINE_VCAL}
        )
        gmail = _mock_gmail_client_returning_ics(
            _INLINE_VCAL.replace(
                "inline-real-uid@google.com", "attachment-uid@google.com"
            ).encode()
        )
        sg = _sg(_build_model_returning(None), gmail, _mock_calendar_client())

        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"].ical_uid == "attachment-uid@google.com"

    def test_unparsable_inline_falls_through_to_llm(self):
        llm_draft = CalendarEventDraft(
            title="fallback",
            start=datetime(2026, 9, 30, 14, 0),
            end=datetime(2026, 9, 30, 15, 0),
        )
        model = _build_model_returning(llm_draft)
        sg = _sg(model, MagicMock(), _mock_calendar_client())

        email = self._email_with_inline_ics().model_copy(
            update={
                "ics_inline": "BEGIN:VCALENDAR\nEND:VCALENDAR\n",
                # keep a date-signal body so the LLM short-circuit
                # doesn't trigger before the fallback
                "body_plain": "活動時間 2026-09-30 14:00-15:00",
            }
        )
        out = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })
        assert out["extracted"] is not None
        assert out["extracted"].title == "fallback"

    def test_inline_uid_reaches_layer_one_dedup(self):
        """End-to-end within the subgraph nodes: inline .ics UID flows
        into `_create_node`'s Layer 1 lookup — the Round B smoke gap."""
        hit = ExistingEvent(
            id="manual-event",
            title="Test Event",
            start=datetime(2026, 9, 30, 6, 0, tzinfo=timezone.utc),
            end=datetime(2026, 9, 30, 7, 0, tzinfo=timezone.utc),
        )
        calendar_client = _mock_calendar_client(ical_uid_hits=[hit])
        sg = _sg(_build_model_returning(None), MagicMock(), calendar_client)

        email = self._email_with_inline_ics()
        extracted = sg._extract_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": None,
        })["extracted"]
        out = sg._create_node({
            "email": email, "analysis": None,
            "side_effect": None, "extracted": extracted,
        })
        assert out["side_effect"].reason == "duplicate_by_ical_uid"
        calendar_client.list_events_by_ical_uid.assert_called_once_with(
            "inline-real-uid@google.com"
        )
        calendar_client.insert_event.assert_not_called()
