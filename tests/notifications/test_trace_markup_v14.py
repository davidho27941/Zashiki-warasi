"""v1.4: `build_notify_markup` composing calendar + trace-copy buttons."""

from __future__ import annotations

from zashiki_warasi.notifications._trace_markup import (
    build_notify_markup,
    build_trace_copy_markup,
)


_TRACE_ID = "a9641b8e136aeb7df9d2d7b85427936d"
_CALENDAR_URL = "https://calendar.google.com/calendar/u/0/r/eventedit/xyz"


class TestBuildNotifyMarkup:
    def test_trace_only_matches_legacy_v13_shape(self):
        """No calendar URL → identical to v1.3 build_trace_copy_markup."""
        markup = build_notify_markup(trace_id=_TRACE_ID, calendar_url=None)
        legacy = build_trace_copy_markup(_TRACE_ID)
        assert markup == legacy

    def test_calendar_and_trace_two_rows_calendar_first(self):
        """Calendar URL button on row 0, trace-copy on row 1 (spec)."""
        markup = build_notify_markup(
            trace_id=_TRACE_ID, calendar_url=_CALENDAR_URL
        )
        assert markup is not None
        rows = markup["inline_keyboard"]
        assert len(rows) == 2

        # Row 0: calendar URL button
        assert rows[0][0]["text"] == "🔗 View in Calendar"
        assert rows[0][0]["url"] == _CALENDAR_URL

        # Row 1: trace-copy button
        assert rows[1][0]["text"] == "📋 Copy trace ID"
        assert rows[1][0]["copy_text"]["text"] == _TRACE_ID

    def test_calendar_only_when_trace_id_none(self):
        markup = build_notify_markup(
            trace_id=None, calendar_url=_CALENDAR_URL
        )
        assert markup is not None
        rows = markup["inline_keyboard"]
        assert len(rows) == 1
        assert rows[0][0]["text"] == "🔗 View in Calendar"

    def test_calendar_only_when_trace_id_malformed(self):
        # Zero sentinel + non-hex + wrong length all yield trace_markup=None
        markup = build_notify_markup(
            trace_id="0" * 32, calendar_url=_CALENDAR_URL
        )
        assert markup is not None
        rows = markup["inline_keyboard"]
        assert len(rows) == 1

    def test_returns_none_when_both_missing(self):
        assert build_notify_markup(trace_id=None, calendar_url=None) is None
        assert build_notify_markup(trace_id="0" * 32, calendar_url=None) is None
