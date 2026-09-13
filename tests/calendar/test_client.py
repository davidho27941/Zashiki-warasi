"""v1.4: GoogleCalendarClient — mocked googleapiclient responses."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import httplib2
import pytest
from googleapiclient.errors import HttpError

from zashiki_warasi.calendar.client import (
    CalendarError,
    CalendarScopeNotGranted,
    GoogleCalendarClient,
    iCalUidExists,
)


def _http_error(status: int, content: bytes = b"") -> HttpError:
    """Build an HttpError with a specific status code."""
    resp = httplib2.Response({"status": str(status), "reason": ""})
    return HttpError(resp, content)


def _fake_service(behavior: dict) -> MagicMock:
    """Assemble a MagicMock that mimics googleapiclient's chained builder.

    `behavior` maps method-path strings to return values (or exceptions).
    E.g. {"freebusy.query.execute": {...}, "events.insert.execute": {...}}.
    """
    service = MagicMock()

    def _apply(path: str, method: MagicMock):
        result = behavior.get(path)
        if isinstance(result, Exception):
            method.side_effect = result
        else:
            method.return_value = result

    _apply(
        "freebusy.query.execute",
        service.freebusy.return_value.query.return_value.execute,
    )
    _apply(
        "events.list.execute",
        service.events.return_value.list.return_value.execute,
    )
    _apply(
        "events.insert.execute",
        service.events.return_value.insert.return_value.execute,
    )
    return service


def _client(service: MagicMock) -> GoogleCalendarClient:
    with patch("zashiki_warasi.calendar.client.build", return_value=service):
        return GoogleCalendarClient(credentials=MagicMock())


# ---- check_free_busy ----------------------------------------------------


class TestCheckFreeBusy:
    def test_empty_when_no_busy_blocks(self):
        service = _fake_service(
            {"freebusy.query.execute": {"calendars": {"primary": {"busy": []}}}}
        )
        client = _client(service)

        result = client.check_free_busy(
            datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
        )
        assert result == []

    def test_returns_busy_slots(self):
        service = _fake_service({
            "freebusy.query.execute": {
                "calendars": {
                    "primary": {
                        "busy": [
                            {
                                "start": "2026-09-15T14:00:00+00:00",
                                "end": "2026-09-15T14:30:00+00:00",
                            }
                        ]
                    }
                }
            }
        })
        client = _client(service)

        result = client.check_free_busy(
            datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
        )
        assert len(result) == 1
        assert result[0].start.hour == 14
        assert result[0].end.minute == 30

    def test_403_raises_scope_missing(self):
        service = _fake_service({
            "freebusy.query.execute": _http_error(403, b"scope missing")
        })
        client = _client(service)
        with pytest.raises(CalendarScopeNotGranted):
            client.check_free_busy(
                datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            )

    def test_500_raises_generic_calendar_error(self):
        service = _fake_service({
            "freebusy.query.execute": _http_error(500, b"server error")
        })
        client = _client(service)
        with pytest.raises(CalendarError) as excinfo:
            client.check_free_busy(
                datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
                datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
            )
        # Not a scope error and not a duplicate — falls through to generic
        assert not isinstance(excinfo.value, CalendarScopeNotGranted)
        assert not isinstance(excinfo.value, iCalUidExists)

    def test_requires_tz_aware_datetimes(self):
        service = _fake_service({})
        client = _client(service)
        with pytest.raises(ValueError, match="tz-aware"):
            client.check_free_busy(
                datetime(2026, 9, 15, 14, 0),  # naive!
                datetime(2026, 9, 15, 15, 0),
            )


# ---- list_events_in_window ---------------------------------------------


class TestListEvents:
    def test_extracts_titles_and_times(self):
        service = _fake_service({
            "events.list.execute": {
                "items": [
                    {
                        "id": "evt-1",
                        "summary": "Weekly Sync",
                        "start": {"dateTime": "2026-09-15T14:00:00+00:00"},
                        "end": {"dateTime": "2026-09-15T14:30:00+00:00"},
                    }
                ]
            }
        })
        client = _client(service)

        result = client.list_events_in_window(
            datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
        )
        assert len(result) == 1
        assert result[0].title == "Weekly Sync"
        assert result[0].id == "evt-1"

    def test_skips_all_day_events(self):
        # All-day events have `date`, not `dateTime` — we skip them
        # (no visual collision with a specific-time meeting).
        service = _fake_service({
            "events.list.execute": {
                "items": [
                    {
                        "id": "all-day",
                        "summary": "Vacation Day",
                        "start": {"date": "2026-09-15"},
                        "end": {"date": "2026-09-16"},
                    },
                    {
                        "id": "timed",
                        "summary": "Meeting",
                        "start": {"dateTime": "2026-09-15T14:00:00+00:00"},
                        "end": {"dateTime": "2026-09-15T15:00:00+00:00"},
                    },
                ]
            }
        })
        client = _client(service)

        result = client.list_events_in_window(
            datetime(2026, 9, 15, 14, 0, tzinfo=timezone.utc),
            datetime(2026, 9, 15, 15, 0, tzinfo=timezone.utc),
        )
        assert len(result) == 1
        assert result[0].id == "timed"


# ---- insert_event -------------------------------------------------------


class TestInsertEvent:
    _payload = {
        "summary": "Test Event",
        "status": "tentative",
        "iCalUID": "test-uid@zashiki.local",
        "start": {"dateTime": "2026-09-15T14:00:00+08:00", "timeZone": "Asia/Taipei"},
        "end": {"dateTime": "2026-09-15T15:00:00+08:00", "timeZone": "Asia/Taipei"},
    }

    def test_success_returns_inserted_event(self):
        service = _fake_service({
            "events.insert.execute": {
                "id": "gcal-evt-abc",
                "iCalUID": "test-uid@zashiki.local",
                "htmlLink": "https://calendar.google.com/event?eid=xxx",
            }
        })
        client = _client(service)

        result = client.insert_event(self._payload)
        assert result.id == "gcal-evt-abc"
        assert result.ical_uid == "test-uid@zashiki.local"
        assert result.view_url.startswith("https://calendar.google.com/")

    def test_missing_ical_uid_raises_value_error(self):
        service = _fake_service({})
        client = _client(service)
        with pytest.raises(ValueError, match="iCalUID"):
            client.insert_event({"summary": "no uid"})

    def test_409_maps_to_ical_uid_exists(self):
        service = _fake_service({
            "events.insert.execute": _http_error(409, b"duplicate uid")
        })
        client = _client(service)
        with pytest.raises(iCalUidExists):
            client.insert_event(self._payload)

    def test_403_maps_to_scope_not_granted(self):
        service = _fake_service({
            "events.insert.execute": _http_error(403, b"scope missing")
        })
        client = _client(service)
        with pytest.raises(CalendarScopeNotGranted):
            client.insert_event(self._payload)

    def test_fallback_view_url_when_no_htmllink(self):
        service = _fake_service({
            "events.insert.execute": {
                "id": "gcal-evt-no-link",
                "iCalUID": "test-uid@zashiki.local",
                # htmlLink absent
            }
        })
        client = _client(service)
        result = client.insert_event(self._payload)
        assert "eventedit/gcal-evt-no-link" in result.view_url
