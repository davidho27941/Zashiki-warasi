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


# ---- v1.7.0 dedup lookups -------------------------------------------------


class TestListEventsByIcalUid:
    def test_empty_response(self):
        service = _fake_service({"events.list.execute": {"items": []}})
        client = _client(service)
        assert client.list_events_by_ical_uid("uid@example.com") == []

    def test_single_hit_mapped(self):
        service = _fake_service({
            "events.list.execute": {
                "items": [{
                    "id": "ev-1",
                    "summary": "產品週會",
                    "start": {"dateTime": "2026-09-15T14:00:00+08:00"},
                    "end": {"dateTime": "2026-09-15T15:00:00+08:00"},
                }]
            }
        })
        client = _client(service)
        hits = client.list_events_by_ical_uid("uid@example.com")
        assert len(hits) == 1
        assert hits[0].id == "ev-1"
        assert hits[0].title == "產品週會"
        # Query used Google's native iCalUID param, deleted excluded.
        kwargs = service.events.return_value.list.call_args.kwargs
        assert kwargs["iCalUID"] == "uid@example.com"
        assert kwargs["showDeleted"] is False

    def test_all_day_event_still_counts(self):
        """A duplicate is a duplicate even when it's all-day — the
        `date`-only shape must NOT be silently dropped like in the
        conflict-summary path."""
        service = _fake_service({
            "events.list.execute": {
                "items": [{
                    "id": "ev-allday",
                    "summary": "conference",
                    "start": {"date": "2026-09-15"},
                    "end": {"date": "2026-09-16"},
                }]
            }
        })
        client = _client(service)
        hits = client.list_events_by_ical_uid("uid@example.com")
        assert len(hits) == 1

    def test_http_error_maps_to_calendar_error(self):
        service = _fake_service({
            "events.list.execute": _http_error(500, b"boom"),
        })
        client = _client(service)
        with pytest.raises(CalendarError):
            client.list_events_by_ical_uid("uid@example.com")

    def test_403_maps_to_scope_not_granted(self):
        service = _fake_service({
            "events.list.execute": _http_error(403),
        })
        client = _client(service)
        with pytest.raises(CalendarScopeNotGranted):
            client.list_events_by_ical_uid("uid@example.com")


class TestListEventsByPrivateExtendedProperty:
    def test_query_shape(self):
        service = _fake_service({"events.list.execute": {"items": []}})
        client = _client(service)
        client.list_events_by_private_extended_property(
            "zwFingerprintV1",
            "a" * 40,
            time_min=datetime(2026, 9, 28, 9, 0, tzinfo=timezone.utc),
            time_max=datetime(2026, 9, 30, 9, 0, tzinfo=timezone.utc),
        )
        kwargs = service.events.return_value.list.call_args.kwargs
        assert kwargs["privateExtendedProperty"] == (
            "zwFingerprintV1=" + "a" * 40
        )
        assert kwargs["timeMin"] == "2026-09-28T09:00:00+00:00"
        assert kwargs["timeMax"] == "2026-09-30T09:00:00+00:00"
        assert kwargs["maxResults"] == 10

    def test_multi_hit_mapped(self):
        service = _fake_service({
            "events.list.execute": {
                "items": [
                    {
                        "id": f"ev-{i}",
                        "start": {"dateTime": "2026-09-29T09:00:00+00:00"},
                        "end": {"dateTime": "2026-09-29T10:00:00+00:00"},
                    }
                    for i in range(2)
                ]
            }
        })
        client = _client(service)
        hits = client.list_events_by_private_extended_property(
            "zwFingerprintV1",
            "b" * 40,
            time_min=datetime(2026, 9, 28, tzinfo=timezone.utc),
            time_max=datetime(2026, 9, 30, tzinfo=timezone.utc),
        )
        assert [h.id for h in hits] == ["ev-0", "ev-1"]
        assert hits[0].title == "(no title)"

    def test_naive_window_rejected(self):
        service = _fake_service({"events.list.execute": {"items": []}})
        client = _client(service)
        with pytest.raises(ValueError, match="tz-aware"):
            client.list_events_by_private_extended_property(
                "zwFingerprintV1",
                "c" * 40,
                time_min=datetime(2026, 9, 28),  # naive
                time_max=datetime(2026, 9, 30, tzinfo=timezone.utc),
            )

    def test_http_error_maps_to_calendar_error(self):
        service = _fake_service({
            "events.list.execute": _http_error(503, b"unavailable"),
        })
        client = _client(service)
        with pytest.raises(CalendarError):
            client.list_events_by_private_extended_property(
                "zwFingerprintV1",
                "d" * 40,
                time_min=datetime(2026, 9, 28, tzinfo=timezone.utc),
                time_max=datetime(2026, 9, 30, tzinfo=timezone.utc),
            )
