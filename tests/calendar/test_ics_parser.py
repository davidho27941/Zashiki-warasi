"""v1.4: `.ics` attachment parser covering common invite shapes."""

from __future__ import annotations

from datetime import datetime, timezone

from zashiki_warasi.agents.verticals._ics_parser import parse_ics_bytes


# ---- Fixtures: real-world .ics shapes ----


def _outlook_single_instance() -> bytes:
    """Outlook-style single VEVENT with explicit TZID (Asia/Tokyo)."""
    return b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
METHOD:REQUEST
BEGIN:VTIMEZONE
TZID:Tokyo Standard Time
BEGIN:STANDARD
DTSTART:16010101T000000
TZOFFSETFROM:+0900
TZOFFSETTO:+0900
END:STANDARD
END:VTIMEZONE
BEGIN:VEVENT
UID:040000008200E00074C5B7101A82E00800000000@outlook.com
DTSTART;TZID=Tokyo Standard Time:20260915T140000
DTEND;TZID=Tokyo Standard Time:20260915T150000
SUMMARY:Product Weekly
LOCATION:Shibuya Office
DESCRIPTION:Weekly sync with product team.
ATTENDEE;CN=Alice Chen;RSVP=TRUE:mailto:alice@example.com
ATTENDEE;CN=Bob Wu:mailto:bob@example.com
END:VEVENT
END:VCALENDAR
"""


def _google_single_instance() -> bytes:
    """Google Calendar-style VEVENT with UTC times + Asia/Taipei TZID."""
    return b"""BEGIN:VCALENDAR
PRODID:-//Google Inc//Google Calendar 70.9054//EN
VERSION:2.0
CALSCALE:GREGORIAN
METHOD:REQUEST
BEGIN:VEVENT
DTSTART:20260920T060000Z
DTEND:20260920T070000Z
DTSTAMP:20260901T000000Z
UID:abc123-real-google-uid@google.com
SUMMARY:Design Review
LOCATION:https://meet.google.com/xxx-yyyy-zzz
DESCRIPTION:Review the v1.4 design.
END:VEVENT
END:VCALENDAR
"""


def _floating_time() -> bytes:
    """Event with no TZID (floating time) — caller applies default."""
    return b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:floating-test@zashiki.local
DTSTART:20260915T140000
DTEND:20260915T150000
SUMMARY:Local Meeting
END:VEVENT
END:VCALENDAR
"""


def _with_rrule() -> bytes:
    """Recurring event — v1.4 handles first instance + logs WARNING."""
    return b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:recurring@zashiki.local
DTSTART;TZID=Asia/Taipei:20260915T140000
DTEND;TZID=Asia/Taipei:20260915T150000
SUMMARY:Weekly Standup
RRULE:FREQ=WEEKLY;BYDAY=TU
END:VEVENT
END:VCALENDAR
"""


def _no_vevent() -> bytes:
    """Malformed .ics with no VEVENT — parser returns None."""
    return b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VTIMEZONE
TZID:Asia/Taipei
END:VTIMEZONE
END:VCALENDAR
"""


def _malformed() -> bytes:
    return b"this is not an ics file at all"


# ---- Tests ----


class TestOutlookInvite:
    def test_parses_summary_and_uid(self):
        draft = parse_ics_bytes(_outlook_single_instance())
        assert draft is not None
        assert draft.title == "Product Weekly"
        assert draft.ical_uid == (
            "040000008200E00074C5B7101A82E00800000000@outlook.com"
        )

    def test_extracts_location(self):
        draft = parse_ics_bytes(_outlook_single_instance())
        assert draft.location == "Shibuya Office"

    def test_extracts_attendee_names(self):
        draft = parse_ics_bytes(_outlook_single_instance())
        assert "Alice Chen" in draft.attendee_names
        assert "Bob Wu" in draft.attendee_names
        # Emails must NOT be included (design: names-only for context;
        # never used to auto-invite).
        assert not any("@" in n for n in draft.attendee_names)

    def test_start_end_are_tz_aware(self):
        draft = parse_ics_bytes(_outlook_single_instance())
        assert draft.start.tzinfo is not None
        assert draft.end.tzinfo is not None


class TestGoogleInvite:
    def test_parses_utc_times(self):
        draft = parse_ics_bytes(_google_single_instance())
        assert draft is not None
        assert draft.start == datetime(2026, 9, 20, 6, 0, 0, tzinfo=timezone.utc)
        assert draft.end == datetime(2026, 9, 20, 7, 0, 0, tzinfo=timezone.utc)

    def test_extracts_meeting_url_in_location(self):
        # Google puts the Meet URL in LOCATION (not a separate field).
        draft = parse_ics_bytes(_google_single_instance())
        assert "meet.google.com" in draft.location


class TestFloatingTime:
    def test_attaches_default_timezone(self):
        draft = parse_ics_bytes(_floating_time(), default_timezone="Asia/Taipei")
        assert draft is not None
        assert draft.start.tzinfo is not None
        # Attached tz should be the default we passed in.
        assert draft.timezone_hint == "Asia/Taipei"


class TestRecurring:
    def test_rrule_sets_has_rrule_flag(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.WARNING):
            draft = parse_ics_bytes(_with_rrule())
        assert draft is not None
        assert draft.has_rrule is True
        assert any("RRULE" in msg for msg in caplog.messages)


class TestFallbacks:
    def test_no_vevent_returns_none(self):
        assert parse_ics_bytes(_no_vevent()) is None

    def test_malformed_returns_none(self):
        assert parse_ics_bytes(_malformed()) is None

    def test_empty_bytes_returns_none(self):
        assert parse_ics_bytes(b"") is None
