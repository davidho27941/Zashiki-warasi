"""Calendar vertical: extract event, create tentative on Google Calendar.

Mirror of expense.py's shape:
    extract → create → END

`extract` prefers `.ics` attachment parsing (deterministic, RFC 5545)
and falls back to an LLM extraction call when no `.ics` is present
(design D1). `create` checks free/busy on the operator's primary
calendar, includes conflict summary in the event description, and
POSTs `events.insert` with `status: "tentative"` (design D2, D3).

All events are created as TENTATIVE — operator confirms/declines
in Google Calendar UI. `calendar_sg` never sends RSVP.

Missing OAuth scope (403) is caught once per pod lifetime and
degrades gracefully to notify (design D4).
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from zashiki_warasi.agents.verticals._ics_parser import parse_ics_bytes
from zashiki_warasi.calendar.client import (
    CalendarError,
    CalendarScopeNotGranted,
    GoogleCalendarClient,
    iCalUidExists,
)
from zashiki_warasi.core.logging import bind_message_context, node_trace
from zashiki_warasi.core.schemas import (
    CalendarConflict,
    CalendarCreated,
    CalendarDuplicate,
    CalendarEventDraft,
    CalendarSkipped,
    EmailAnalysis,
    EmailMessage,
    SideEffect,
)
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.observability import llm_calls_total, llm_latency_seconds
from zashiki_warasi.observability.instrumentation import (
    record_call,
    set_gen_ai_attributes,
    zashiki_span,
)

logger = logging.getLogger(__name__)


CALENDAR_ICS_MIME = "text/calendar"


CALENDAR_EXTRACT_SYSTEM_PROMPT = """\
你是行事曆事件擷取助理。閱讀使用者提供的電子郵件(可能包含會議 /
講座 / 活動邀請),把結構化欄位輸出出來。若沒有 .ics 附件,你就是
唯一資訊來源。

規則:

1. 信件可能是繁體中文、簡體中文、英文或日文。

2. **必填欄位**(缺任一 → 全部回 null,不猜):
   - `title` 事件名稱 (30 字內中文簡短描述)
   - `start` 開始時間,**只包含日期時間、不含時區**:
     `YYYY-MM-DDTHH:MM:SS`(切勿加 `Z`、`+08:00` 或任何 offset 後綴)
   - `end` 結束時間,同 `start` 格式(不含時區)

3. 選填欄位(信件未明確提及請回 null):
   - `location` 實體地點 (e.g.「台北市信義區信義路五段 7 號」、
     「Zoom 會議室」、「Google Meet」)
   - `description` 事件說明 / 議程,原信摘要即可,50-200 字
   - `attendee_names` 出席者姓名陣列 (**只**擷取名字,不要 email)

4. **時區判斷**(絕對別把 offset 混進 `start`/`end`):
   - `start` / `end` 一律是 naive wall-clock time,對應在該事件當地時區
   - 信件明確標示時區(JST、UTC+9、Asia/Tokyo)→ 放 `timezone_hint`
     (IANA 名稱如 `Asia/Tokyo`),`start`/`end` 用該時區當地時間
   - 信件無時區線索 → `timezone_hint` 空著,系統套用 `Asia/Taipei`
   - 若信中時間是「當地時間 19:30 台北」→ start = `2026-09-17T19:30:00`,
     timezone_hint = `Asia/Taipei`

5. **反幻想極重要**:
   - 若信件只是「未來會有活動,細節後續通知」→ 全部回 null
   - 若信件是**取消通知** / **改期通知** → 全部回 null(v1.4 不處理
     invitation lifecycle,取消/改期由人手處理)
   - 不要根據 subject / sender 猜測時間或地點

6. **格式**:回 JSON;缺欄位用 null,不要用空字串。
"""


class CalendarState(TypedDict):
    """Subgraph state. `extracted` is subgraph-internal; other fields
    merge back into parent AgentState on exit."""

    email: EmailMessage
    analysis: EmailAnalysis | None
    side_effect: SideEffect | None
    extracted: CalendarEventDraft | None


class CalendarSubgraph:
    """Calendar vertical packaged for symmetry with ExpenseSubgraph.

    `.graph` is a compiled StateGraph the parent wires via
    `add_node("calendar_sg", sg.graph)`.
    """

    def __init__(
        self,
        *,
        checkpointer: PostgresSaver | None,
        client: GmailClient,
        calendar_client: GoogleCalendarClient,
        model: BaseChatModel,
        default_timezone: str = "Asia/Taipei",
        llm_model_name: str = "unknown",
        llm_system: str = "unknown",
    ) -> None:
        self._client = client
        self._calendar_client = calendar_client
        self._default_timezone = default_timezone
        self._structured_model = model.with_structured_output(CalendarEventDraft)
        self._llm_model_name = llm_model_name
        self._llm_system = llm_system
        # Per-instance latch: we log the "calendar scope not granted"
        # WARNING exactly ONCE for this subgraph's lifetime to avoid
        # flooding logs when every subsequent calendar-worthy email
        # hits the same 403. Instance attribute (not module-global)
        # per v1.1 D25 pattern — see docs/v1.1-implementation-notes.md.
        self._scope_missing_logged = False
        self.graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver | None):
        builder = StateGraph(CalendarState)
        builder.add_node("extract", self._extract_node)
        builder.add_node("create", self._create_node)
        builder.add_edge(START, "extract")
        builder.add_edge("extract", "create")
        builder.add_edge("create", END)
        return builder.compile(checkpointer=checkpointer)

    # ----- nodes -----

    def _log(
        self, state: CalendarState
    ) -> logging.Logger | logging.LoggerAdapter:
        email = state.get("email")
        if email is None:
            return logger
        return bind_message_context(logger, message_id=email.id)

    def _extract_node(self, state: CalendarState) -> dict:
        log = self._log(state)
        with node_trace(log, "calendar.extract"):
            email = state["email"]

            # ---- .ics attachment first (deterministic) ----
            ics_att = _find_ics_attachment(email)
            if ics_att is not None:
                log.info(f"calendar: parsing .ics attachment {ics_att.filename!r}")
                try:
                    data = self._client.get_attachment(email.id, ics_att.attachment_id)
                except Exception as exc:  # noqa: BLE001 - defensive
                    log.warning(
                        f"calendar: failed to download .ics ({exc}); "
                        "falling through to LLM extraction"
                    )
                else:
                    draft = parse_ics_bytes(data, self._default_timezone)
                    if draft is not None:
                        log.info(
                            f"calendar: .ics extracted title={draft.title!r} "
                            f"start={draft.start.isoformat()} "
                            f"uid={draft.ical_uid!r}"
                        )
                        return {"extracted": draft}
                    log.info("calendar: .ics parse returned None → LLM fallback")

            # ---- LLM extraction (fallback) ----
            body = (
                email.body_plain
                or email.body_html
                or email.snippet
                or ""
            )
            if not body.strip():
                log.info("calendar: no body text to extract from → skip")
                return {
                    "extracted": None,
                    "side_effect": CalendarSkipped(
                        reason="extraction_failed",
                        detail="no body text and no .ics attachment",
                    ),
                }

            user_prompt = (
                f"From: {email.from_address}\n"
                f"Subject: {email.subject}\n"
                f"Date: {email.received_at.isoformat()}\n\n"
                f"{body}"
            )
            with zashiki_span("llm.chat") as _span, record_call(
                counter=llm_calls_total,
                histogram=llm_latency_seconds,
                counter_labels={"node": "calendar_extract"},
                histogram_labels={"node": "calendar_extract"},
            ):
                set_gen_ai_attributes(
                    _span,
                    system=self._llm_system,
                    model=self._llm_model_name,
                )
                try:
                    draft: CalendarEventDraft | None = (
                        self._structured_model.invoke(
                            [
                                SystemMessage(
                                    content=CALENDAR_EXTRACT_SYSTEM_PROMPT
                                ),
                                HumanMessage(content=user_prompt),
                            ]
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - defensive
                    log.warning(
                        f"calendar: LLM extraction failed ({exc}); "
                        "falling through to no event"
                    )
                    return {
                        "extracted": None,
                        "side_effect": CalendarSkipped(
                            reason="extraction_failed",
                            detail=f"llm error: {exc.__class__.__name__}",
                        ),
                    }
                set_gen_ai_attributes(
                    _span,
                    system=self._llm_system,
                    model=self._llm_model_name,
                    response=draft,
                )

            if draft is None:
                log.info("calendar: LLM returned no draft → skip")
                return {
                    "extracted": None,
                    "side_effect": CalendarSkipped(
                        reason="extraction_failed",
                        detail="LLM returned no draft",
                    ),
                }

            log.info(
                f"calendar: LLM extracted title={draft.title!r} "
                f"start={draft.start.isoformat()}"
            )
            return {"extracted": draft}

    def _create_node(self, state: CalendarState) -> dict:
        log = self._log(state)
        with node_trace(log, "calendar.create"):
            # Skip if extract already set a terminal side_effect.
            if state.get("side_effect") is not None:
                return {}

            draft = state.get("extracted")
            if draft is None:
                return {
                    "side_effect": CalendarSkipped(
                        reason="extraction_failed",
                        detail="no draft returned from extract",
                    ),
                }

            email = state["email"]
            ical_uid = draft.ical_uid or _synthesize_ical_uid(email.id)

            # ---- Free/busy check ----
            conflicts: list[CalendarConflict] = []
            try:
                busy = self._calendar_client.check_free_busy(
                    draft.start, draft.end
                )
                if busy:
                    events = self._calendar_client.list_events_in_window(
                        draft.start, draft.end
                    )
                    conflicts = [
                        CalendarConflict(
                            title=ev.title, start=ev.start, end=ev.end
                        )
                        for ev in events
                    ]
                    log.info(f"calendar: {len(conflicts)} time conflict(s)")
            except CalendarScopeNotGranted as exc:
                return self._degrade_scope_missing(log, str(exc))
            except CalendarError as exc:
                log.warning(
                    f"calendar: free/busy check failed ({exc}); "
                    "proceeding with insert without conflict info"
                )

            # ---- Build payload ----
            payload = _build_insert_payload(
                draft, email.id, ical_uid, conflicts
            )

            # ---- Insert ----
            try:
                inserted = self._calendar_client.insert_event(payload)
            except iCalUidExists:
                log.info(
                    f"calendar: iCalUID {ical_uid!r} already present "
                    "(idempotent no-op)"
                )
                return {
                    "side_effect": CalendarDuplicate(ical_uid=ical_uid),
                }
            except CalendarScopeNotGranted as exc:
                return self._degrade_scope_missing(log, str(exc))
            except CalendarError as exc:
                log.warning(f"calendar: insert failed ({exc})")
                return {
                    "side_effect": CalendarSkipped(
                        reason="extraction_failed",
                        detail=f"insert error: {exc.__class__.__name__}",
                    ),
                }

            log.info(
                f"calendar: created tentative event {inserted.id} "
                f"({inserted.view_url})"
            )
            return {
                "side_effect": CalendarCreated(
                    event_id=inserted.id,
                    view_url=inserted.view_url,
                    title=draft.title,
                    start=draft.start,
                    end=draft.end,
                    location=draft.location,
                    conflicts=conflicts,
                ),
            }

    # ----- helpers -----

    def _degrade_scope_missing(self, log, message: str) -> dict:
        """Log ONCE per subgraph instance lifetime, degrade to skipped.

        Instance-latch (`self._scope_missing_logged`) instead of a
        module-global — test isolation is automatic (each test builds
        a fresh subgraph), no shared mutable state across pods with
        multiple subgraphs, and no need for a `monkeypatch` fixture
        to reset a module var between tests. Matches v1.1 D25's move
        away from `global _FACTORY_INSTALLED` toward attribute-based
        sentinels.
        """
        if not self._scope_missing_logged:
            log.warning(
                f"calendar: OAuth scope not granted ({message}); "
                "degrading to notify-only for the rest of this subgraph's "
                "lifetime. Reauth via /reauth to grant the calendar "
                "scope (full)."
            )
            self._scope_missing_logged = True
        return {
            "side_effect": CalendarSkipped(
                reason="scope_missing",
                detail="calendar scope not granted; run /reauth",
            ),
        }


# ---- Module helpers -----------------------------------------------------


def _find_ics_attachment(email: EmailMessage):
    """Return the first `text/calendar` attachment, or None."""
    for att in email.attachments:
        if att.mime_type.lower().startswith(CALENDAR_ICS_MIME):
            return att
    return None


def _synthesize_ical_uid(message_id: str) -> str:
    """RFC 5545-compliant UID when the source has none.

    Format keeps the message_id as the local part so the operator can
    trace an event back to the email that spawned it (also persisted
    as `extendedProperties.private.zashiki_message_id` on the event).
    """
    return f"zashiki-{message_id}@zashiki-warasi.local"


def _build_insert_payload(
    draft: CalendarEventDraft,
    message_id: str,
    ical_uid: str,
    conflicts: list[CalendarConflict],
) -> dict:
    """Compose the Google Calendar events.insert JSON body.

    Always includes `status: "tentative"` (design D2). Description
    embeds any conflict summary (design D3). extendedProperties keep
    the source message_id for post-hoc traceability (design D5).

    Timezone handling: Google Calendar's `dateTime` with a UTC offset
    (e.g. `+00:00`, `Z`) OVERRIDES the `timeZone` field for placement.
    Any tz-aware draft datetime is first converted to the target zone
    (`timezone_hint` or `Asia/Taipei`), then stripped of tzinfo so
    the emitted `dateTime` is a naive wall-clock string — Google then
    honors `timeZone` for placement. This makes the payload robust to
    LLM outputs that stray a `Z` onto the datetime string.
    """
    tz_name = draft.timezone_hint or "Asia/Taipei"
    tz = _load_zone(tz_name)
    start_iso = _naive_iso_in_tz(draft.start, tz)
    end_iso = _naive_iso_in_tz(draft.end, tz)
    description_parts: list[str] = []
    if draft.description:
        description_parts.append(draft.description)

    if conflicts:
        conflict_lines = ["", "⚠️ Time conflicts:"]
        for c in conflicts[:3]:
            local_start = c.start.strftime("%H:%M")
            local_end = c.end.strftime("%H:%M")
            conflict_lines.append(f"   - {local_start}-{local_end} {c.title}")
        if len(conflicts) > 3:
            conflict_lines.append(f"   - …and {len(conflicts) - 3} more")
        description_parts.append("\n".join(conflict_lines))

    description_parts.append(
        f"\n[Auto-created from email {message_id} by Zashiki-warasi]"
    )
    description = "\n".join(description_parts).strip()

    payload: dict = {
        "summary": draft.title,
        "status": "tentative",
        "iCalUID": ical_uid,
        "start": {
            "dateTime": start_iso,
            "timeZone": tz_name,
        },
        "end": {
            "dateTime": end_iso,
            "timeZone": tz_name,
        },
        "description": description,
        "extendedProperties": {
            "private": {
                "zashiki_message_id": message_id,
            },
        },
    }
    if draft.location:
        payload["location"] = draft.location
    return payload


def _load_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        logger.warning(
            f"calendar: unknown timezone {name!r}, falling back to Asia/Taipei"
        )
        return ZoneInfo("Asia/Taipei")


def _naive_iso_in_tz(dt: datetime, tz: ZoneInfo) -> str:
    """Emit an offset-free ISO 8601 string in the target zone.

    If `dt` is tz-aware it is first converted to `tz`; either way the
    output has no offset so Google honors the payload's `timeZone`
    field for placement (design: dateTime-with-offset overrides
    timeZone; strip the offset to prevent LLM-stray-Z bugs).
    """
    if dt.tzinfo is not None:
        dt = dt.astimezone(tz)
    return dt.replace(tzinfo=None).isoformat()
