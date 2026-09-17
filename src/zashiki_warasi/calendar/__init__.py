"""Google Calendar side-effect module.

Wraps the calendar-vertical's write path against Google's Calendar
API v3. The `calendar-vertical` capability's `_create_node` consumes
`GoogleCalendarClient`; the .ics parser + LLM extractor live in
`zashiki_warasi.agents.verticals.calendar` (as with expense).
"""

from zashiki_warasi.calendar.client import (
    BusySlot,
    CalendarError,
    CalendarScopeNotGranted,
    ExistingEvent,
    GoogleCalendarClient,
    InsertedEvent,
    iCalUidExists,
)

__all__ = [
    "BusySlot",
    "CalendarError",
    "CalendarScopeNotGranted",
    "ExistingEvent",
    "GoogleCalendarClient",
    "InsertedEvent",
    "iCalUidExists",
]
