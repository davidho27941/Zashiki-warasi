"""RFC 5545 `.ics` attachment parser for the calendar-vertical.

`.ics` extraction is preferred over LLM extraction when the email
carries a `text/calendar` attachment — the fields are already
structured and 100% accurate (design D1).

Handles the common shapes:
- Outlook / Google Calendar single-instance invites (VEVENT with
  DTSTART, DTEND, SUMMARY, LOCATION, UID)
- Emails with a VTIMEZONE definition attached
- RRULE-bearing events (v1.4: first occurrence only; caller logs
  a WARNING when this fires)

Doesn't handle:
- METHOD:CANCEL / METHOD:REPLY — these are invitation lifecycle
  events, not new-event creation; would need cross-email state
  (backlog).
- All-day events without a specific time — returned as start/end at
  local midnight, caller decides.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from icalendar import Calendar

from zashiki_warasi.core.schemas import CalendarEventDraft

logger = logging.getLogger(__name__)


def parse_ics_bytes(
    data: bytes,
    default_timezone: str = "Asia/Taipei",
) -> CalendarEventDraft | None:
    """Parse an `.ics` payload; return the first VEVENT as a draft, or None.

    `default_timezone` is used when the event has floating time (no
    TZID) or when TZID resolution fails.
    """
    try:
        cal = Calendar.from_ical(data)
    except (ValueError, KeyError, TypeError) as exc:
        logger.warning(f"ics: failed to parse ({exc.__class__.__name__}: {exc})")
        return None

    # Walk components; take the first VEVENT. Real .ics files may
    # contain VTIMEZONE + one VEVENT; multi-VEVENT files (batch
    # exports) are rare in email invites — take the first, log.
    events = [c for c in cal.walk() if c.name == "VEVENT"]
    if not events:
        logger.info("ics: no VEVENT in payload")
        return None
    if len(events) > 1:
        logger.info(f"ics: {len(events)} VEVENTs — using the first")

    vevent = events[0]

    start_raw = vevent.get("dtstart")
    end_raw = vevent.get("dtend")
    summary = vevent.get("summary")
    if start_raw is None or end_raw is None or summary is None:
        logger.info("ics: VEVENT missing dtstart/dtend/summary")
        return None

    start, tz_hint_start = _resolve_datetime(start_raw.dt, default_timezone)
    end, tz_hint_end = _resolve_datetime(end_raw.dt, default_timezone)
    tz_hint = tz_hint_start or tz_hint_end

    rrule = vevent.get("rrule")
    has_rrule = rrule is not None
    if has_rrule:
        logger.warning(
            "ics: VEVENT has RRULE (recurring event); v1.4 handles the "
            "first occurrence only. Consider manual duplication if the "
            "operator needs the full series."
        )

    return CalendarEventDraft(
        title=str(summary),
        start=start,
        end=end,
        location=_str_or_none(vevent.get("location")),
        description=_str_or_none(vevent.get("description")),
        ical_uid=_str_or_none(vevent.get("uid")),
        attendee_names=_extract_attendee_names(vevent),
        timezone_hint=tz_hint,
        has_rrule=has_rrule,
    )


# ---- Helpers ------------------------------------------------------------


def _resolve_datetime(
    value: datetime | date,
    default_timezone: str,
) -> tuple[datetime, str | None]:
    """Convert an icalendar dt to a tz-aware datetime + timezone label.

    icalendar returns:
    - `datetime` with tzinfo when TZID is present and resolvable
    - `datetime` naive when the .ics has floating time (no TZID)
    - `date` for all-day events — coerced to midnight of that date

    Returned tzinfo is always non-None; `tz_hint` is the recovered
    timezone label (or the default) for downstream display.
    """
    tz_hint: str | None = None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # Floating time — attach the operator's default.
            tz = _load_zone(default_timezone)
            tz_hint = default_timezone
            return value.replace(tzinfo=tz), tz_hint
        # Already tz-aware.
        tz_hint = _describe_tzinfo(value.tzinfo)
        return value, tz_hint
    # It's a date (all-day event) — coerce to midnight in default tz.
    tz = _load_zone(default_timezone)
    dt = datetime.combine(value, time.min, tzinfo=tz)
    return dt, default_timezone


def _load_zone(name: str):  # -> tzinfo, but py version friendly
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning(f"ics: unknown timezone {name!r}, falling back to UTC")
        return timezone.utc


def _describe_tzinfo(tz) -> str | None:
    """Best-effort tzinfo → IANA name string for hint reporting."""
    key = getattr(tz, "key", None)  # ZoneInfo has .key
    if key:
        return key
    return str(tz) if tz else None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _extract_attendee_names(vevent) -> list[str]:
    """Pull attendee CNs (common names). Names only — never used to
    auto-invite; just for context in the notify template."""
    attendees_raw = vevent.get("attendee")
    if attendees_raw is None:
        return []
    # icalendar returns a single value or a list depending on count.
    values = attendees_raw if isinstance(attendees_raw, list) else [attendees_raw]
    names: list[str] = []
    for entry in values:
        params = getattr(entry, "params", {}) or {}
        cn = params.get("CN")
        if cn:
            names.append(str(cn).strip())
    return names
