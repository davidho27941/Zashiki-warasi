"""Google Calendar API v3 client wrapper for the calendar-vertical.

Three read/write concerns:

- `check_free_busy(start, end)` — freebusy.query for the operator's
  primary calendar; returns busy blocks overlapping the window.
  Used before create to surface conflicts (design D3).
- `list_events_in_window(start, end)` — events.list for the same
  window; called only when freebusy reported busy, to fetch the
  overlapping events' titles for the conflict summary.
- `insert_event(payload)` — events.insert with `iCalUID` for
  idempotent creation (design D5). Payload always includes
  `status: "tentative"` (design D2) — the wrapper doesn't enforce
  this, the caller does; keeping the sink policy-free.

Error mapping:

- 403 Forbidden → `CalendarScopeNotGranted` (operator hasn't reauth'd
  with the calendar scope). Caller degrades gracefully to notify-only.
- 409 Conflict on iCalUID → `iCalUidExists` (already-present event;
  idempotent no-op for the caller).
- Other 4xx/5xx → `CalendarError` with body attached.
- Transport-level failures (network) bubble as `httplib2` /
  `googleapiclient` exceptions; caller decides retry policy.

We do NOT retry inside the client — googleapiclient's built-in
`num_retries` handles 429/500/503 with exponential backoff when the
caller opts in. For our use, retries would delay the tick body; better
to let a failed insert fail the tick, checkpoint the analyze result,
and let the next `/poll` invocation resume from notify.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ---- Exception hierarchy --------------------------------------------------


class CalendarError(Exception):
    """Base for all Google Calendar interaction errors we surface."""


class CalendarScopeNotGranted(CalendarError):
    """403 from the API — the OAuth credential lacks the calendar scope (full).

    Raised by any read/write call. Caller (calendar_sg) catches this
    once per pod lifetime, logs at WARNING, and degrades to notify.
    """


class iCalUidExists(CalendarError):  # noqa: N801 — Google's field name
    """409 Conflict from events.insert — the iCalUID is already present.

    Idempotent success for the caller (event already in calendar).
    Carries the pre-existing event's id + view URL when Google returns
    them; else None.
    """

    def __init__(
        self,
        message: str,
        existing_event_id: str | None = None,
        view_url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.existing_event_id = existing_event_id
        self.view_url = view_url


# ---- Return-type dataclasses ---------------------------------------------


@dataclass(frozen=True)
class BusySlot:
    """A single busy block returned by freebusy.query."""

    start: datetime
    end: datetime


@dataclass(frozen=True)
class ExistingEvent:
    """An event returned by events.list — used to build the conflict summary."""

    id: str
    title: str
    start: datetime
    end: datetime


@dataclass(frozen=True)
class InsertedEvent:
    """The tentative event we just created."""

    id: str
    ical_uid: str
    view_url: str
    """The `htmlLink` from Google's response — direct link to Calendar UI."""


# ---- The client ----------------------------------------------------------


class GoogleCalendarClient:
    """Thin wrapper over `googleapiclient.discovery.build('calendar', 'v3')`.

    Constructed once per app lifetime with the OAuth `Credentials`
    already refreshed by the Gmail auth path — we share the same
    credential; scope-level auth is enforced by Google server-side.
    """

    def __init__(
        self,
        credentials: Credentials,
        primary_calendar_id: str = "primary",
    ) -> None:
        # `cache_discovery=False` — the googleapiclient tries to write
        # a discovery cache to ~/.cache by default. Non-root containers
        # can't write there and the noise obscures real errors.
        self._service = build(
            "calendar",
            "v3",
            credentials=credentials,
            cache_discovery=False,
        )
        self._primary_id = primary_calendar_id

    # ---- freebusy --------------------------------------------------------

    def check_free_busy(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[BusySlot]:
        """Return busy blocks overlapping [start, end] on the target calendar.

        Empty list = fully free. Times are returned in the timezone
        Google reports (usually UTC); caller converts for display.
        """
        target = calendar_id or self._primary_id
        body = {
            "timeMin": _iso(start),
            "timeMax": _iso(end),
            "items": [{"id": target}],
        }
        try:
            response = self._service.freebusy().query(body=body).execute()
        except HttpError as exc:
            raise self._map_http_error(exc) from exc

        calendars = response.get("calendars", {})
        busy_blocks = calendars.get(target, {}).get("busy", [])
        return [
            BusySlot(
                start=_parse_iso(block["start"]),
                end=_parse_iso(block["end"]),
            )
            for block in busy_blocks
        ]

    # ---- events.list -----------------------------------------------------

    def list_events_in_window(
        self,
        start: datetime,
        end: datetime,
        calendar_id: str | None = None,
    ) -> list[ExistingEvent]:
        """Return events overlapping [start, end] with titles + times.

        Called only when `check_free_busy` reported busy blocks — we
        pay for the fuller payload only when we need the titles for
        the conflict summary.
        """
        target = calendar_id or self._primary_id
        try:
            response = (
                self._service.events()
                .list(
                    calendarId=target,
                    timeMin=_iso(start),
                    timeMax=_iso(end),
                    singleEvents=True,
                    orderBy="startTime",
                    maxResults=25,
                )
                .execute()
            )
        except HttpError as exc:
            raise self._map_http_error(exc) from exc

        events: list[ExistingEvent] = []
        for item in response.get("items", []):
            start_obj = _extract_datetime(item.get("start"))
            end_obj = _extract_datetime(item.get("end"))
            if start_obj is None or end_obj is None:
                # All-day events return `date` (no time) — skip for
                # conflict-summary purposes; they don't visually
                # collide with a specific-time meeting.
                continue
            events.append(
                ExistingEvent(
                    id=item["id"],
                    title=item.get("summary", "(no title)"),
                    start=start_obj,
                    end=end_obj,
                )
            )
        return events

    # ---- events.insert ---------------------------------------------------

    def insert_event(
        self,
        payload: dict[str, Any],
        calendar_id: str | None = None,
    ) -> InsertedEvent:
        """POST events.insert with idempotent iCalUID.

        Payload MUST include `iCalUID` (top-level, not inside the body
        — that's Google's flat parameter). Caller is responsible for
        `status: "tentative"`, description, start/end, etc.

        Returns `InsertedEvent` on 200. Raises `iCalUidExists` on 409
        (treat as success by caller — event already there). Raises
        `CalendarScopeNotGranted` on 403.
        """
        target = calendar_id or self._primary_id
        ical_uid = payload.get("iCalUID")
        if not ical_uid:
            raise ValueError(
                "payload must include 'iCalUID' for idempotent insert"
            )
        # Google's insert accepts iCalUID as a query-string param
        # (which lets it dedupe against pre-existing events with the
        # same UID). We ALSO leave it in the body so both paths agree.
        try:
            response = (
                self._service.events()
                .insert(
                    calendarId=target,
                    body=payload,
                    conferenceDataVersion=0,
                    supportsAttachments=False,
                )
                .execute()
            )
        except HttpError as exc:
            mapped = self._map_http_error(exc)
            if isinstance(mapped, iCalUidExists):
                # Enrich with the existing event's link when Google
                # tells us the collision id (it usually doesn't — the
                # 409 body is a generic error). We can list-events to
                # look it up, but that's a second API call for a case
                # our caller treats as no-op success. Skip enrichment.
                raise mapped from exc
            raise mapped from exc

        return InsertedEvent(
            id=response["id"],
            ical_uid=response.get("iCalUID", ical_uid),
            view_url=response.get(
                "htmlLink",
                _fallback_view_url(response["id"]),
            ),
        )

    # ---- error mapping ---------------------------------------------------

    @staticmethod
    def _map_http_error(exc: HttpError) -> CalendarError:
        status = getattr(exc.resp, "status", None)
        if status == 403:
            return CalendarScopeNotGranted(
                "Google Calendar API returned 403 — the OAuth credential "
                "lacks the calendar scope (full). Reauth via /reauth to "
                "grant it (see docs/calendar-vertical.md)."
            )
        if status == 409:
            return iCalUidExists(
                "Google Calendar API returned 409 — an event with this "
                "iCalUID already exists (idempotent no-op for caller)."
            )
        return CalendarError(
            f"Google Calendar API returned {status}: {exc.content!r}"
        )


# ---- Helpers -------------------------------------------------------------


def _iso(dt: datetime) -> str:
    """RFC 3339 timestamp Google expects. Preserves tz if present."""
    if dt.tzinfo is None:
        raise ValueError(
            "GoogleCalendarClient requires tz-aware datetimes; "
            f"got naive {dt!r}. Attach a timezone before calling."
        )
    return dt.isoformat()


def _parse_iso(value: str) -> datetime:
    """Parse an RFC 3339 timestamp Google returned."""
    # Python 3.13's fromisoformat handles the trailing Z since 3.11.
    return datetime.fromisoformat(value)


def _extract_datetime(field: dict | None) -> datetime | None:
    """Google returns `start`/`end` as {"dateTime": "...", "timeZone": "..."} or {"date": "..."}."""
    if field is None:
        return None
    if "dateTime" in field:
        return _parse_iso(field["dateTime"])
    # All-day event — skip; caller filters None.
    return None


def _fallback_view_url(event_id: str) -> str:
    """When Google doesn't return `htmlLink`, build a best-effort URL.

    The base64url-encoded eventId form works even for events across
    multiple linked accounts; `/u/0/` means "primary account" which is
    correct when the operator logged in with a single Google account.
    Design Q2 flags the multi-account edge case for smoke verification.
    """
    return (
        "https://calendar.google.com/calendar/u/0/r/eventedit/"
        f"{event_id}"
    )
