"""LangGraph email agent: analyze + (optional) expense vertical + notify.

Graph:

    START → analyze ─┬─ expense category  → expense_sg ──┐
                     ├─ calendar category → calendar_sg ─┤
                     └─ otherwise ───────────────────────┴→ notify → END

Which categories are expense / calendar depends on the taxonomy
version (`classifier/taxonomy.py`).

Each email runs in its own thread (thread_id = email.id). The expense
subgraph shares the same thread, so a crash mid-subgraph resumes at the
right node on the next tick (no LLM re-call, no Telegram re-send).
"""

from __future__ import annotations

import html
import logging
from datetime import datetime, timezone
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from openai import BadRequestError, LengthFinishReasonError
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.agents.verticals.calendar import CalendarSubgraph
from zashiki_warasi.agents.verticals.expense import ExpenseSubgraph
from zashiki_warasi.calendar.client import GoogleCalendarClient
from zashiki_warasi.classifier.laya_shadow_client import LayaShadowClient
from zashiki_warasi.classifier.taxonomy import get_taxonomy
from zashiki_warasi.core.config import CalendarSettings, TaxonomySettings
from zashiki_warasi.agents.verticals.html_text import html_to_text
from zashiki_warasi.core.config import LLMSettings
from zashiki_warasi.core.logging import bind_message_context, node_trace
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import (
    AnalysisFailed,
    CalendarCreated,
    CalendarDuplicate,
    CalendarSkipped,
    EmailAnalysis,
    EmailMessage,
    ExpenseLogged,
    ExpenseNeedsReview,
    SideEffect,
    coerce_importance,
)
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.notifications._trace_markup import (
    build_notify_markup,
    build_trace_copy_markup,
)
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.telegram import TelegramNotifier
from zashiki_warasi.observability import (
    graph_span,
    llm_call,
    llm_calls_total,
    llm_latency_seconds,
    observe_email_end_to_end,
)
from zashiki_warasi.observability.instrumentation import (
    record_call,
    set_gen_ai_attributes,
)

logger = logging.getLogger(__name__)


def _current_trace_id_hex() -> str | None:
    """Read the active OTel span's trace_id as 32-char lowercase hex.

    Returns None on any failure — no active span, malformed span context,
    SDK missing — so the notify path can never break on this read.
    """
    try:
        from opentelemetry import trace

        ctx = trace.get_current_span().get_span_context()
        return f"{ctx.trace_id:032x}"
    except Exception:
        return None


def _current_trace_copy_markup() -> dict | None:
    """v1.3 legacy: build a copy-trace-id-only markup.

    Kept for the AnalysisFailed / no-analysis notify path which has no
    calendar side-effect (single button is fine). Newer paths use
    `build_notify_markup` directly to compose calendar + trace buttons.
    """
    return build_trace_copy_markup(_current_trace_id_hex())


# Pre-v2 name kept for docs and ad-hoc scripts; the analyze node reads
# the prompt from the active taxonomy entry.
ANALYZE_SYSTEM_PROMPT = get_taxonomy("v1").system_prompt


# ----- prompt-context-overflow detection -----
#
# `openai.BadRequestError` (HTTP 400) fires for many unrelated reasons
# — bad auth, bad tool spec, schema violations. We only translate the
# "your prompt exceeded my context window" subset into AnalysisFailed;
# every other 400 MUST propagate so we notice new failure classes fast.
#
# Signal priority (first match wins) — structural fields are
# preferred; string-match fallback is intentionally last so a
# false-positive requires both (a) an unrelated 400 and (b) its
# message happening to contain "context" + a length word.

_CONTEXT_LENGTH_TEXT_MARKERS: tuple[str, ...] = ("length", "size", "window")


def _is_context_length_error(exc: BadRequestError) -> bool:
    """True iff a 400 is specifically "prompt exceeded context window".

    Consulted signals (see design D2):
      1. error.type == "exceed_context_size_error"   (llama-cpp-server)
      2. error.code == "context_length_exceeded"     (OpenAI-official)
      3. message contains "context" + one of length/size/window
    """
    body = getattr(exc, "body", None) or {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        if error.get("type") == "exceed_context_size_error":
            return True
        if error.get("code") == "context_length_exceeded":
            return True
        message = error.get("message") or ""
    else:
        message = str(exc)
    lowered = message.lower()
    if "context" not in lowered:
        return False
    return any(marker in lowered for marker in _CONTEXT_LENGTH_TEXT_MARKERS)


def _extract_context_length_detail(exc: BadRequestError) -> str:
    """Best-effort short summary for AnalysisFailed.detail.

    Prefers llama-cpp-server's structured `n_prompt_tokens` /
    `n_ctx` fields when present; otherwise falls back to the raw
    message (truncated so a giant model error can't blow up the
    Telegram payload).
    """
    body = getattr(exc, "body", None) or {}
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        n_prompt = error.get("n_prompt_tokens")
        n_ctx = error.get("n_ctx")
        if n_prompt is not None and n_ctx is not None:
            return f"prompt={n_prompt} n_ctx={n_ctx}"
        message = error.get("message") or ""
        if message:
            return message[:200]
    return str(exc)[:200]


def _observe_email_e2e(
    email: EmailMessage, category: str, outcome: str
) -> None:
    """Emit one observation into `zashiki_email_end_to_end_duration_seconds`.

    Computed at `_notify` completion as `now - email.received_at`, where
    `received_at` is Gmail's `internalDate` (already UTC-aware — see
    `gmail/client.py`). Naive-datetime guard is defensive: PostgresSaver
    round-trips may strip tzinfo depending on serializer, so we
    treat any naive value as UTC. Clock-skew guard clamps a negative
    delta to 0 rather than corrupting the histogram bucket boundaries.
    """
    now = datetime.now(tz=timezone.utc)
    received = email.received_at
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    duration = (now - received).total_seconds()
    if duration < 0:
        duration = 0.0
    observe_email_end_to_end(category, outcome, duration)


class AgentState(TypedDict):
    email: EmailMessage
    analysis: EmailAnalysis | None
    side_effect: SideEffect | None


class EmailAgent:
    def __init__(
        self,
        *,
        checkpointer: PostgresSaver,
        session_factory: sessionmaker,
        notifier: TelegramNotifier,
        client: GmailClient,
        notion: NotionExpenseRecorder | None = None,
        calendar_client: GoogleCalendarClient | None = None,
        calendar_settings: CalendarSettings | None = None,
        laya_shadow: LayaShadowClient | None = None,
        taxonomy_settings: TaxonomySettings | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._taxonomy = get_taxonomy(
            (taxonomy_settings or TaxonomySettings()).version
        )
        self._notifier = notifier
        # v1.6.0 shadow classifier — inert unless LAYA_SHADOW_ENABLED=1
        # (constructing the default reads env; disabled = zero cost,
        # no laya connectivity, no questions-file read).
        self._laya_shadow = laya_shadow or LayaShadowClient(
            session_factory=session_factory
        )

        # Two model instances against the same server:
        # - analyze: capped by LLM_ANALYZE_MAX_TOKENS. Bounds
        #   degenerate JSON loops so LengthFinishReasonError fires in
        #   ~1s instead of after the whole context window (~30s).
        # - expense extraction: uncapped. The extract JSON can be
        #   larger (multiple items, attachments), and its failure mode
        #   is not the same degenerate-loop risk.
        llm_settings = LLMSettings()
        # Kept as instance state so span attributes can name the model
        # (`gen_ai.request.model`) without re-reading settings on every
        # invoke call.
        self._llm_model_name = llm_settings.model
        self._llm_system = llm_settings.provider
        analyze_chat_model = get_chat_model(
            llm_settings, max_tokens=llm_settings.analyze_max_tokens
        )
        extract_chat_model = get_chat_model(llm_settings)
        self._analyze_model = analyze_chat_model.with_structured_output(
            self._taxonomy.output_model
        )
        self._expense_subgraph = ExpenseSubgraph(
            checkpointer=checkpointer,
            session_factory=session_factory,
            client=client,
            model=extract_chat_model,
            notion=notion,
            llm_model_name=self._llm_model_name,
            llm_system=self._llm_system,
        )

        # v1.4 calendar vertical (opt-in via CalendarSettings.enabled).
        # When disabled OR when construction fails (e.g. calendar_client
        # not passed by caller for backward compat), the vertical is
        # None and _route_by_category routes calendar-worthy categories
        # to notify instead — same as v1.3 behavior.
        self._calendar_settings = calendar_settings or CalendarSettings()
        self._calendar_subgraph: CalendarSubgraph | None = None
        if self._calendar_settings.enabled and calendar_client is not None:
            self._calendar_subgraph = CalendarSubgraph(
                checkpointer=checkpointer,
                client=client,
                calendar_client=calendar_client,
                model=extract_chat_model,
                default_timezone=self._calendar_settings.timezone,
                llm_model_name=self._llm_model_name,
                llm_system=self._llm_system,
            )

        self._graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver):
        builder = StateGraph(AgentState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("expense_sg", self._expense_subgraph.graph)
        builder.add_node("notify", self._notify)

        # Calendar node only when the subgraph was constructed.
        # `_route_by_category` returns "calendar" iff self._calendar_subgraph
        # is set AND the category matches.
        routes = {"expense": "expense_sg", "notify": "notify"}
        if self._calendar_subgraph is not None:
            builder.add_node("calendar_sg", self._calendar_subgraph.graph)
            builder.add_edge("calendar_sg", "notify")
            routes["calendar"] = "calendar_sg"

        builder.add_edge(START, "analyze")
        builder.add_conditional_edges(
            "analyze",
            self._route_by_category,
            routes,
        )
        builder.add_edge("expense_sg", "notify")
        builder.add_edge("notify", END)
        return builder.compile(checkpointer=checkpointer)

    # ----- nodes -----

    def _log(
        self, state: AgentState
    ) -> logging.Logger | logging.LoggerAdapter:
        """Return a LoggerAdapter that stamps `message_id` onto every
        record emitted from inside a node. Nodes always call this at
        the top so `grep message_id=<id>` follows one email's
        lifecycle across module boundaries.

        Falls back to the plain module logger when `state` has no
        `email` — real invocations always carry one (it's set before
        the graph starts), but unit tests that exercise a single
        router in isolation may not.
        """
        email = state.get("email")
        if email is None:
            return logger
        return bind_message_context(logger, message_id=email.id)

    def _analyze(self, state: AgentState) -> dict:
        log = self._log(state)
        with node_trace(log, "analyze"), graph_span("analyze", "email") as gs:
            email = state["email"]
            # Body fallback chain: text/plain → HTML converted on
            # demand → Gmail snippet. Covers HTML-only mails (modern
            # e-receipts, marketing newsletters) that previously
            # degraded to the ~200-char snippet alone.
            body = (
                email.body_plain
                or html_to_text(email.body_html)
                or email.snippet
                or ""
            )
            user_text = (
                f"From: {email.from_address}\n"
                f"Subject: {email.subject}\n"
                f"Date: {email.received_at.isoformat()}\n"
                f"\n"
                f"{body}"
            )
            try:
                with llm_call(
                    "classify", model=self._llm_model_name
                ) as _lc, record_call(
                    counter=llm_calls_total,
                    histogram=llm_latency_seconds,
                    counter_labels={"node": "analyze"},
                    histogram_labels={"node": "analyze"},
                ):
                    set_gen_ai_attributes(
                        _lc.span,
                        system=self._llm_system,
                        model=self._llm_model_name,
                    )
                    output = self._analyze_model.invoke(
                        [
                            SystemMessage(
                                content=self._taxonomy.system_prompt
                            ),
                            HumanMessage(content=user_text),
                        ]
                    )
                    # Structured-output wrappers strip token usage from
                    # the returned pydantic instance; set_gen_ai_attributes
                    # is a no-op on absent usage metadata, so this call is
                    # safe even when there's nothing to attach.
                    set_gen_ai_attributes(
                        _lc.span,
                        system=self._llm_system,
                        model=self._llm_model_name,
                        response=output,
                    )
            except LengthFinishReasonError as exc:
                usage = exc.completion.usage
                detail = (
                    f"prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens}"
                )
                log.warning(f"analyze: LLM hit token limit ({detail})")
                gs.outcome = "error"
                return {
                    "analysis": None,
                    "side_effect": AnalysisFailed(
                        reason="content_too_long", detail=detail
                    ),
                }
            except BadRequestError as exc:
                # HTTP 400 covers many unrelated failures (auth, bad
                # tool spec, schema violations) — narrow to only the
                # prompt-context-overflow subset so real bugs still
                # propagate loudly to the poller's outer handler.
                if not _is_context_length_error(exc):
                    raise
                detail = _extract_context_length_detail(exc)
                log.warning(f"analyze: prompt exceeds context ({detail})")
                # Cheap forensics: raw error body at DEBUG so post-
                # incident triage has the server's own wording.
                log.debug(f"analyze: 400 body = {getattr(exc, 'body', None)}")
                gs.outcome = "error"
                return {
                    "analysis": None,
                    "side_effect": AnalysisFailed(
                        reason="prompt_too_long", detail=detail
                    ),
                }
            analysis = None
            if output is not None:
                analysis = EmailAnalysis(
                    **{
                        **output.model_dump(),
                        "taxonomy_version": self._taxonomy.version,
                    }
                )
                log.info(f"classified as {analysis.category}")
                # v1.6.0 shadow: fire-and-forget, never raises, never
                # read by the pipeline. AnalysisFailed paths return
                # earlier — no llm_category means no shadow sample.
                self._laya_shadow.classify_async(
                    email=email, llm_category=analysis.category
                )
            return {"analysis": analysis}

    def _route_by_category(self, state: AgentState) -> str:
        log = self._log(state)
        analysis = state.get("analysis")
        if analysis is None:
            log.info("routing to notify (no analysis)")
            return "notify"
        # Route by the analysis' own taxonomy, not the active one: an
        # analysis checkpointed under v1 keeps v1 routing after cutover.
        vertical = get_taxonomy(analysis.taxonomy_version).vertical_for(
            analysis.category
        )
        if vertical == "expense":
            log.info(f"routing to expense (category={analysis.category})")
            return "expense"
        # `getattr` defensive: unit tests exercise `_route_by_category`
        # by constructing agents via `__new__` (no `__init__`), so
        # `_calendar_subgraph` may not exist. Route calendar-worthy
        # categories to notify in that case (v1.3 behavior).
        calendar_sg = getattr(self, "_calendar_subgraph", None)
        if calendar_sg is not None and vertical == "calendar":
            log.info(f"routing to calendar (category={analysis.category})")
            return "calendar"
        log.info(f"routing to notify (category={analysis.category})")
        return "notify"

    def _notify(self, state: AgentState) -> dict:
        log = self._log(state)
        with node_trace(log, "notify"), graph_span("notify", "email") as gs:
            analysis = state["analysis"]
            side_effect = state.get("side_effect")
            markup = _current_trace_copy_markup()
            email = state["email"]
            if analysis is None:
                # Analyze itself failed — no structured summary to
                # render. If it left us an AnalysisFailed marker we
                # still ping the user (they need to open the mail
                # manually); otherwise we log and skip since something
                # else went off-script.
                if isinstance(side_effect, AnalysisFailed):
                    text = _format_analysis_failed(
                        email, side_effect
                    )
                    self._notifier.send_message(text, reply_markup=markup)
                    log.info("notified user of analyze failure")
                    _observe_email_e2e(email, "unknown", "error")
                    return {}
                log.warning("notify: skipping — no analysis")
                gs.outcome = "skipped"
                _observe_email_e2e(email, "unknown", "skipped")
                return {}
            text = _format_message(email, analysis, side_effect)
            # v1.4: when the side_effect is a CalendarCreated, add a
            # "View in Calendar" URL button alongside the trace-copy
            # button. Non-calendar paths reuse the trace-only markup.
            if isinstance(side_effect, CalendarCreated):
                markup = build_notify_markup(
                    trace_id=_current_trace_id_hex(),
                    calendar_url=side_effect.view_url,
                )
            self._notifier.send_message(text, reply_markup=markup)
            log.info("notified user")
            _observe_email_e2e(email, analysis.category, "success")
            return {}

    # ----- entry point -----

    def handle_email(self, email: EmailMessage) -> None:
        """Run the agent on one email and persist the analysis.

        Idempotency contract — three cases keyed by `email.id`:
          - No prior state: invoke the graph fresh.
          - Prior state, graph completed: reuse cached values, skip
            invoke (no LLM re-call, no Telegram re-send).
          - Prior state, graph interrupted: invoke with no input so
            LangGraph resumes from the last completed node.
        """
        config = {"configurable": {"thread_id": email.id}}
        snapshot = self._graph.get_state(config)

        if snapshot.values.get("analysis") is not None and not snapshot.next:
            analysis = snapshot.values["analysis"]
            logger.info(
                f"Reusing cached analysis for {email.id} "
                "(graph already complete)"
            )
        else:
            graph_input = (
                None
                if snapshot.values
                else {"email": email, "analysis": None, "side_effect": None}
            )
            result = self._graph.invoke(graph_input, config=config)
            analysis = result.get("analysis")

        if analysis is None:
            logger.warning(f"Agent returned no analysis for {email.id}")
            return
        self._persist(email.id, analysis)
        logger.info(
            f"Analyzed {email.id}: "
            f"category={analysis.category} "
            f"importance={analysis.importance} "
            f"urgency={analysis.urgency}"
        )

    def _persist(self, message_id: str, analysis: EmailAnalysis) -> None:
        # Same defensive coercion as _format_message — state loaded
        # from a LangGraph checkpoint may have bypassed the
        # field_validator and arrive here with a string importance.
        importance = coerce_importance(analysis.importance)
        if not isinstance(importance, int):
            importance = 3

        with self._session_factory() as session:
            if session.get(EmailAnalysisORM, message_id) is not None:
                return
            session.add(
                EmailAnalysisORM(
                    message_id=message_id,
                    importance=importance,
                    urgency=analysis.urgency,
                    category=analysis.category,
                    summary=analysis.summary,
                    keywords=list(analysis.keywords),
                )
            )
            session.commit()


# ----- formatting -----


_URGENCY_LABEL = {
    "very_urgent": "非常緊急",
    "urgent": "緊急",
    "normal": "普通",
    "none": "沒有急迫性",
}


def _format_message(
    email: EmailMessage,
    analysis: EmailAnalysis,
    side_effect: SideEffect | None,
) -> str:
    """Telegram HTML payload, aligned with the spec output template.

    All user-controlled fields are HTML-escaped.
    """
    # Defensive: LangGraph's checkpoint deserializer uses
    # `model_construct`, which bypasses pydantic validators, so a
    # cached state with `importance` as a string can reach here even
    # though the EmailAnalysis validator would normally coerce it.
    importance = coerce_importance(analysis.importance)
    if not isinstance(importance, int):
        importance = 3  # opaque fallback
    stars = "★" * importance + "☆" * (5 - importance)
    parts: list[str] = [
        f"<b>{stars} [{html.escape(analysis.category)}]</b>",
        "",
        f"<b>標題:</b> {html.escape(email.subject)}",
        f"<b>寄件者:</b> {html.escape(email.from_address)}",
        "",
        "<b>內容摘要:</b>",
        html.escape(analysis.summary),
        "",
        f"<b>急迫性:</b> {_URGENCY_LABEL[analysis.urgency]}",
    ]

    if side_effect is not None:
        parts.append("")
        if side_effect.kind == "expense":
            parts.append(_format_expense_logged(side_effect))
        elif side_effect.kind == "expense_needs_review":
            parts.append(_format_expense_needs_review(side_effect))
        elif side_effect.kind == "calendar_created":
            parts.append(_format_calendar_created(side_effect))
        elif side_effect.kind == "calendar_duplicate":
            parts.append(_format_calendar_duplicate(side_effect))
        elif side_effect.kind == "calendar_skipped":
            parts.append(_format_calendar_skipped(side_effect))

    if analysis.keywords:
        parts.append("")
        tags = " ".join(f"#{html.escape(k)}" for k in analysis.keywords)
        parts.append(f"<b>關鍵字:</b> {tags}")

    return "\n".join(parts)


def _format_expense_logged(effect: ExpenseLogged) -> str:
    """Render every payment field. Missing values show as 不明 so the
    user sees a complete frame rather than guessing whether a field was
    "not present in email" vs. "we forgot to display it"."""
    header = "💰 <b>已記帳</b>"
    if effect.title:
        header += f": {html.escape(effect.title)}"
    lines = [header]

    if effect.amount is not None:
        amt = f"{effect.amount} {effect.currency or ''}".strip()
    else:
        amt = "不明"
    lines.append(f"  金額: {html.escape(amt)}")

    lines.append(f"  商家: {html.escape(effect.vendor or '不明')}")
    lines.append(f"  地點: {html.escape(effect.location or '不明')}")
    lines.append(f"  類別: {html.escape(effect.category or '不明')}")

    if effect.transacted_at:
        time_str = f"{effect.transacted_at:%Y-%m-%d %H:%M}"
    else:
        time_str = "不明"
    lines.append(f"  時間: {time_str}")

    if effect.payment_method == "其他":
        lines.append("  支付: ⚠️ 其他 (請檢查信件確認)")
    elif effect.payment_method:
        lines.append(f"  支付: {html.escape(effect.payment_method)}")
    else:
        lines.append("  支付: 不明")

    if effect.transaction_id:
        suffix = (
            " (自動編號)"
            if effect.transaction_id.startswith("AUTO-")
            else ""
        )
        lines.append(
            f"  編號: <code>{html.escape(effect.transaction_id)}</code>"
            f"{suffix}"
        )
    else:
        # persist always sets transaction_id (real or AUTO-), so we
        # should never hit this branch in practice.
        lines.append("  編號: 不明")

    # Notion mirror status. notion_page_id and notion_sync_error are
    # mutually exclusive in normal operation; when both are None the
    # Notion integration is simply not configured and we render
    # nothing.
    if effect.notion_page_id:
        page_id_no_dashes = effect.notion_page_id.replace("-", "")
        lines.append(
            f"  🔗 https://notion.so/{page_id_no_dashes}"
        )
    elif effect.notion_sync_error:
        err = html.escape(effect.notion_sync_error[:80])
        lines.append(f"  ⚠️ Notion 同步失敗: {err}")

    return "\n".join(lines)


def _format_analysis_failed(
    email: EmailMessage, effect: AnalysisFailed
) -> str:
    """Render an `AnalysisFailed` — no analysis body exists yet, so
    this is the ENTIRE Telegram message (unlike ExpenseLogged /
    NeedsReview which piggyback onto the normal analysis block)."""
    lines = [
        "⚠️ <b>LLM 分析失敗</b>",
        "",
        f"<b>標題:</b> {html.escape(email.subject)}",
        f"<b>寄件者:</b> {html.escape(email.from_address)}",
        "",
    ]
    # Reason-specific one-liner so the operator knows which physical
    # failure hit (output truncation vs. input overflow — different
    # remediation). Fall-through covers any future variant so the
    # message never comes out with zero context.
    if effect.reason == "content_too_long":
        lines.append(
            "原因: LLM 產出被截斷 (finish_reason=length),"
            "結構化分析未完成。"
        )
    elif effect.reason == "prompt_too_long":
        lines.append(
            "原因: 郵件內容超出 LLM 上下文視窗 (HTTP 400),"
            "無法送出分析請求。"
        )
    else:
        lines.append(f"原因: {html.escape(effect.reason)}")
    if effect.detail:
        lines.append(f"用量: <code>{html.escape(effect.detail)}</code>")
    lines.append("→ 請打開原信手動處理。")
    return "\n".join(lines)


def _format_expense_needs_review(effect: ExpenseNeedsReview) -> str:
    lines = ["⚠️ <b>消費通知需人工檢查</b>"]
    if effect.reason == "image_pdf_unreadable":
        lines.append("PDF 附件為影像格式,無法自動抽取支付資訊。")
        if effect.unreadable_attachments:
            files = ", ".join(
                html.escape(f) for f in effect.unreadable_attachments
            )
            lines.append(f"附件: {files}")
    elif effect.reason == "extraction_yielded_nulls":
        lines.append("信件內容不足以擷取明確支付資訊。")
    lines.append("→ 請打開原信手動處理。")
    return "\n".join(lines)


# ----- v1.4 calendar-vertical formatters -----


def _format_calendar_created(effect: CalendarCreated) -> str:
    """Render the created tentative event + any conflict summary.

    The `🔗 View in Calendar` URL button is added as a `reply_markup`
    on the send, NOT rendered as a link here — buttons are more
    tappable than inline links on mobile Telegram.
    """
    lines = [
        f"📅 <b>事件已建立 (tentative):</b> {html.escape(effect.title)}",
        f"  時間: {effect.start:%Y-%m-%d %H:%M} – {effect.end:%H:%M}",
    ]
    if effect.location:
        lines.append(f"  📍 {html.escape(effect.location)}")

    if effect.conflicts:
        lines.append("")
        lines.append("⚠️ <b>該時段已有事件:</b>")
        for c in effect.conflicts[:3]:
            lines.append(
                f"  • {c.start:%H:%M}-{c.end:%H:%M} {html.escape(c.title)}"
            )
        remaining = len(effect.conflicts) - 3
        if remaining > 0:
            lines.append(f"  • …還有 {remaining} 個事件")

    return "\n".join(lines)


def _format_calendar_duplicate(effect: CalendarDuplicate) -> str:
    return (
        "📅 <b>事件已存在:</b> 這封信對應的行事曆事件已於稍早建立,"
        "略過重複建立。"
    )


def _format_calendar_skipped(effect: CalendarSkipped) -> str:
    # Duplicate skips are GOOD news (the event is already on the
    # calendar), so they get a friendlier header than the failure
    # reasons below — distinct from no_event_signal/extraction_failed.
    if effect.reason in ("duplicate_by_ical_uid", "duplicate_by_fingerprint"):
        lines = ["📅 <b>已跳過:</b> 此事件已存在於行事曆"]
        if effect.detail:
            lines.append(f"  說明: {html.escape(effect.detail)}")
        return "\n".join(lines)
    reason_zh = {
        "scope_missing": "尚未授權 Google Calendar (請 /reauth)",
        "extraction_failed": "無法擷取完整事件資訊",
        "disabled": "行事曆整合已停用 (CALENDAR_ENABLED=0)",
        "no_event_signal": "內文無明確活動時間",
    }.get(effect.reason, effect.reason)
    lines = [f"📅 <b>行事曆事件未建立:</b> {reason_zh}"]
    if effect.detail:
        lines.append(f"  說明: {html.escape(effect.detail)}")
    return "\n".join(lines)
