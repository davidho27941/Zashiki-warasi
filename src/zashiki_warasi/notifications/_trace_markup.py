"""Telegram inline-keyboard helper for the "copy trace ID" button.

Turns an OTel trace_id (32-char lowercase hex) into a Telegram Bot API
`inline_keyboard` payload with a single `copy_text` button. Tapping
that button on a modern Telegram client (>= 11.0) copies the trace ID
to the operator's clipboard so they can paste it into Grafana → Tempo
→ Search by Trace ID and see the full lifespan of the event that
generated the alert.

Kept private to the notifications package (leading underscore) —
callers use `TelegramNotifier` for delivery, not this helper directly.
"""

from __future__ import annotations

_TRACE_ID_HEX_LEN = 32
_ZERO_TRACE_ID = "0" * _TRACE_ID_HEX_LEN
_BUTTON_LABEL = "📋 Copy trace ID"


def build_trace_copy_markup(trace_id: str | None) -> dict | None:
    """Return a Telegram `reply_markup` dict, or None if trace_id is unusable.

    Returns None (caller sends the message without a button) when:
    - `trace_id` is None / empty.
    - `trace_id` is the all-zero sentinel (NoOpTracerProvider returns
      this when `OTEL_ENABLED=0` or no active span).
    - `trace_id` is not exactly 32 lowercase-hex chars (malformed input,
      never expected in practice; guard so callers don't need to).
    """
    if not trace_id or len(trace_id) != _TRACE_ID_HEX_LEN:
        return None
    if trace_id == _ZERO_TRACE_ID:
        return None
    try:
        int(trace_id, 16)
    except ValueError:
        return None
    if trace_id != trace_id.lower():
        return None
    return {
        "inline_keyboard": [
            [
                {
                    "text": _BUTTON_LABEL,
                    "copy_text": {"text": trace_id},
                }
            ]
        ]
    }


_CALENDAR_BUTTON_LABEL = "🔗 View in Calendar"


def build_notify_markup(
    trace_id: str | None,
    calendar_url: str | None = None,
) -> dict | None:
    """Compose the full inline_keyboard for a notify message.

    v1.4 addition: when the notify follows a `calendar_sg` create, an
    extra URL button jumps straight to the Google Calendar UI for that
    event. The `📋 Copy trace ID` button (v1.3) sits **below** the
    calendar button so the calendar-specific action is closer to the
    thumb on mobile (per notifications spec).

    Returns None only when BOTH buttons are unavailable (no trace + no
    calendar url) — text-only message.
    """
    rows: list[list[dict]] = []
    if calendar_url:
        rows.append([{"text": _CALENDAR_BUTTON_LABEL, "url": calendar_url}])
    trace_markup = build_trace_copy_markup(trace_id)
    if trace_markup is not None:
        # trace_markup itself is {"inline_keyboard": [[...]]} — grab
        # its single row and append to ours to keep one flat list.
        rows.extend(trace_markup["inline_keyboard"])
    if not rows:
        return None
    return {"inline_keyboard": rows}
