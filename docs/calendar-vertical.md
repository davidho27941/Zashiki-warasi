# Calendar vertical (v1.4)

> 中文版:[`calendar-vertical.zh.md`](calendar-vertical.zh.md).

When the email agent classifies a message as `會議邀請` or `講座資訊`,
v1.4 routes it to `calendar_sg` — a LangGraph subgraph that
auto-creates a **tentative** Google Calendar event and surfaces
any time-slot conflicts alongside the Telegram alert.

**Design principle: never auto-RSVP.** All events are created with
`status: tentative`. You confirm or decline in Google Calendar's own
UI. `calendar_sg` never sends RSVP responses back to the invite
organizer.

## What triggers it

- Category: `會議邀請` or `講座資訊` (module constant
  `_CALENDAR_LIKE_CATEGORIES` in `zashiki_warasi/agents/email_agent.py`).
- **`講座資訊` boundary (v1.4.1 refinement):** requires BOTH a
  concrete event date/time AND a specific place / online-meeting
  link. Course recommendations (Coursera, Udemy, MOOC newsletters)
  without cohort dates route to `廣告`, NOT `講座資訊`. See the
  analyze system prompt for the anti-example list.
- **Extract short-circuit (v1.4.1 second line of defense):** even
  when a message miscategorizes into `講座資訊`, the extract node
  runs a lightweight regex pre-flight on `subject + body` before
  invoking the LLM. If NO date/time pattern matches
  (`YYYY-MM-DD`, `M/D`, `HH:MM`, weekday names in en/zh, etc.),
  extraction short-circuits with `CalendarSkipped(reason="no_event_signal")`
  — the Telegram alert reads `📅 行事曆事件未建立: 內文無明確活動時間`
  and no LLM call is made. `.ics` attachments always bypass the
  short-circuit (structured data is inherently event-signaled).
- Kill switch: `CALENDAR_ENABLED=0` in `.env` skips the vertical
  entirely — same behavior as v1.3.
- OAuth: needs `https://www.googleapis.com/auth/calendar` scope on
  the same credential Gmail uses. (Not the narrower `calendar.events`
  — `freebusy.query` for conflict detection requires the full scope.)

## Upgrading from v1.3.x

**One-time step: re-auth for the new scope.**

The v1.4 image ships with a broader default scope list (Gmail readonly
+ Calendar events) but Google's OAuth token from v1.3 doesn't include
the new scope. Until you re-auth, any Calendar API call returns 403 —
`calendar_sg` catches it, logs `calendar: OAuth scope not granted`
**once per pod lifetime**, and gracefully degrades to notify-only.

To grant the scope:

```
curl -X POST http://<host>:8080/reauth -H "X-API-Key: $HTTP_API_KEY"
```

Response includes `auth_url`. Open in a browser, review the new
"See and download your calendar events" consent, click Allow.
Google redirects back; `token.json` is updated with the new scope.

That's it — next `POST /poll` that hits a calendar-worthy category
will use the vertical.

## What you'll see

**Telegram alert** (in addition to the standard summary):

```
📅 事件已建立 (tentative): 產品週會
   時間: 2026-09-15 14:00 – 15:00
   📍 信義區辦公室

⚠️ 該時段已有事件:
   • 14:00-14:30 週會
   • 14:00-14:30 1-on-1 with X

[ 🔗 View in Calendar ]      ← inline URL button
[ 📋 Copy trace ID ]         ← v1.3 button, still present
```

Tapping `🔗 View in Calendar` opens the exact event in Google
Calendar UI where you accept / decline / edit. Tapping
`📋 Copy trace ID` copies the trace_id (v1.3 log→trace jump).

**Google Calendar entry**:
- Status: tentative (shown as diagonal-striped on the calendar grid)
- Title, start/end, location from the extraction step
- Description embeds the conflict summary + a "created from email X"
  footer for traceability

## Extraction: `.ics` first, LLM fallback

Modern invites (Outlook, Google Calendar, Zoom) ship a `text/calendar`
attachment (`.ics` file). `calendar_sg` parses it via the `icalendar`
Python library — deterministic, RFC 5545 compliant, 100% accurate.

Only when the email lacks a `.ics` attachment does the vertical fall
back to LLM extraction of the body. The LLM is instructed to return
`null` for any field it can't confidently identify; if the required
`{title, start, end}` set is incomplete, the event is skipped and
you get a text-only Telegram alert with `📅 行事曆事件未建立`.

## Conflict detection

Before `events.insert`, `calendar_sg` calls Google Calendar's
`freebusy.query` for your primary calendar. Any busy blocks
overlapping the target time window are surfaced:

- In the Telegram alert (immediate visibility)
- In the created event's description (persisted for later review in
  Calendar UI)

The event is created as tentative **regardless of conflicts** — you
may want to accept the new invite and decline the conflicting one;
that decision stays with you.

## Config

Three env keys (all optional; defaults suit a homelab operator):

| Env var | Default | Purpose |
|---|---|---|
| `CALENDAR_ENABLED` | `1` | `0` disables the vertical; calendar-worthy categories route to notify (v1.3 behavior). |
| `CALENDAR_TIMEZONE` | `Asia/Taipei` | Applied to events with floating time (no TZID in `.ics`, or LLM extraction with no explicit tz). |
| `CALENDAR_PRIMARY_ID` | `primary` | Google Calendar id for freebusy check + event insert. `primary` = the auth'd account's primary calendar. |

## Idempotency

Same email processed twice (retry, checkpointer replay) never creates
duplicate events. `calendar_sg` computes an `iCalUID` from either the
`.ics` `UID` field or `f"zashiki-{message_id}@zashiki-warasi.local"`;
Google returns 409 Conflict on a second insert with the same UID,
which the vertical treats as a no-op success.

For forwarded / CC'd invites carrying the same real calendar UID,
this gives cross-email dedup for free.

## Inspecting via Grafana

Every `calendar_sg` run produces a span tree in Tempo:

```
POST /poll
└── zashiki.tick_once
    └── zashiki.node.calendar.extract
        └── zashiki.llm.chat            (only when .ics absent)
    └── zashiki.node.calendar.create
        (freebusy, events.list, events.insert are httpx spans
         from auto-instrumentation)
```

Tap the `📋 Copy trace ID` button on the Telegram alert → paste into
Grafana → Tempo → Search by Trace ID → see the full flow from Gmail
history event → analyze → route → calendar extract + create →
Telegram send.

## Disabling

Two paths:

- **Temporarily**: `CALENDAR_ENABLED=0` in `.env` + restart pod. No
  events created; calendar-worthy categories go to notify.
- **Permanently**: revoke the calendar scope via Google Account
  → Security → Third-party apps. The vertical catches 403 and
  degrades gracefully (WARNING logged once per pod).

Existing tentative events in your calendar are untouched by either
option; delete them manually in Calendar UI if desired.

## Non-goals (deferred to future changes)

- Auto-RSVP (accept/decline via Telegram callback button) — requires
  bot polling/webhook infra we don't have.
- Recurring event support beyond first occurrence — RRULE parsing +
  cross-instance management is a bigger scope.
- Multi-calendar conflict check — v1 primary only.
- Attendee auto-invite (as organizer).
- Update-on-invitation-revision — would need cross-email state.

## Related

- OpenSpec change: `openspec/changes/archive/YYYY-MM-DD-add-calendar-vertical/`
  (proposal + design + spec deltas).
- v1.3 log→trace jump: [`observability.md`](observability.md)
  "Telegram Copy trace ID button".
- Expense vertical (parallel pattern):
  `src/zashiki_warasi/agents/verticals/expense.py`.
