"""LangGraph email agent: analyze + (optional) expense vertical + notify.

Graph:

    START → analyze ─┬─ category == "消費支出" → expense_sg ─┐
                     └─ otherwise ──────────────────────────┴→ notify → END

Each email runs in its own thread (thread_id = email.id). The expense
subgraph shares the same thread, so a crash mid-subgraph resumes at the
right node on the next tick (no LLM re-call, no Telegram re-send).
"""

from __future__ import annotations

import html
import logging
from typing import TypedDict

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from openai import BadRequestError, LengthFinishReasonError
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.agents.verticals.expense import ExpenseSubgraph
from zashiki_warasi.agents.verticals.html_text import html_to_text
from zashiki_warasi.core.config import LLMSettings
from zashiki_warasi.core.logging import bind_message_context, node_trace
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import (
    AnalysisFailed,
    EmailAnalysis,
    EmailMessage,
    ExpenseLogged,
    ExpenseNeedsReview,
    SideEffect,
    coerce_importance,
)
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.telegram import TelegramNotifier
from zashiki_warasi.observability import (
    llm_calls_total,
    llm_latency_seconds,
)
from zashiki_warasi.observability.instrumentation import record_call

logger = logging.getLogger(__name__)


ANALYZE_SYSTEM_PROMPT = """\
你是電子郵件分析助理。讀完信件後產出結構化分析,涵蓋以下五項。

### 1. 重要度 (importance, 1-5)

1 = 非常不重要 / 2 = 不重要 / 3 = 普通 / 4 = 重要 / 5 = 非常重要

判斷依據:
- 內容的重要程度
- 訊息的含金量
- 對生活的影響程度
- 對個人資產的影響程度

特例規則:
- 以下類型 **至少 3 分**:科技新知、技術新知、講座訊息、股票資訊
- 以下類型 **最多 3 分**:促銷、廣告、信貸

### 2. 摘要 (summary, 50-200 字)

以 5W1H (誰、什麼、何時、何地、為何、如何) 為核心做摘要。

當郵件內容是關於消費 / 點數 / 帳單彙整時,務必在摘要中提及:
- 消費 / 獲得金額(若是點數則為點數數量)
- 消費 / 獲得時間或期間
- 消費地點 / 獲得來源(店家)
- 交易識別碼、ID 或編號(伝票番号 / 注文番号)
若信件中未提及某項,可以以「不明」替代。

⚠️ 反幻想(重要):摘要中列出的消費/交易必須是郵件中明確、
實際發生的交易 — 具體日期時間 + 店家 + 金額三者齊全才算。
以下 **不算** 實際交易,絕對不要當成一筆消費列進摘要:
- 手續費說明(例如「海外ATMの利用手数料 110円を加える」、
  「送金手数料無料」等條件描述)
- 假設情境或條款(例如「為替影響で変更があった場合...」)
- 廣告、注意事項、活動預告、客服說明

若信件下方有 boilerplate 條款,只摘要真實交易,忽略條款文字。
真實交易缺少的欄位用「不明」填,不要用條款裡的數字補位。

例(好):「楽天ポイントカード 通知使用者 2026/06/18-06/24
期間獲得的點數明細:於『彩家 楽天クリムゾン』獲得 19 點、
於『東急ストア宮崎台店』獲得 3 點,合計 22 點。」

例(不好,太空泛):「樂天點數卡寄送的每週點數獲得通知,
旨在告知使用者所累積的點數總額與明細。」

### 3. 分類 (category)

從以下列表選擇 **一項**:
消費支出、消費資訊彙整、點數資訊彙整、訂閱服務、技術文章、
講座資訊、會議邀請、帳單通知、廣告、促銷、社交、新聞、
安全通知、股票資訊、其他

分類規則:

- 金融產品(基金、ETF、信貸、信用卡帳單分期等)的「推銷」郵件
  一律歸類為「廣告」或「促銷」,不要分到「消費支出」或
  「帳單通知」。
- 點數消費 / 獲得通知(楽天ポイント、d ポイント、悠遊付點數等)
  **不視為「消費支出」**,歸類為「點數資訊彙整」。
- 信用卡多筆消費彙整(一封信包含多則消費資訊、或多筆刷卡紀錄
  的彙總)**不要分類為「消費支出」**,歸類為「消費資訊彙整」。
- 「消費支出」的嚴格定義 — 郵件必須明確包含**至少一筆**同時
  具備以下 **全部四項** 的真實交易:
    (a) 具體的日期時間
    (b) 具體的店家/商戶名稱(**不是**「本人利用」、「カード利用」
        這種通用敘述)
    (c) 金額
    (d) 交易識別碼(承認番号 / 伝票番号 / 注文番号 等)
  四項**缺任何一項**即不算單筆真實消費 → 分類為「消費資訊彙整」。
  * 正例(消費支出):SMBC Olive デビット的「ご利用のお知らせ」
    含 ◇利用日 + ◇利用先 + ◇利用金額 + ◇承認番号 全部四項。
    即便信件下方寫著「海外ATMでの現地通貨の引き出しは...ATM
    利用手数料110円を加えて引き落とし致します」這種條款,仍為
    「消費支出」— 不要把 110 円當成第二筆消費(boilerplate 條款
    不算獨立交易)。
  * 反例(消費資訊彙整):楽天カードの「【速報版】カード利用の
    お知らせ(本人ご利用分)」只有當日消費總額(可能有日期),
    **沒有具體店家名稱、沒有承認番号/注文番号** — 即便金額看起
    來只有一筆,也是彙整摘要,**絕對不要**分類為「消費支出」;
    正確分類是「消費資訊彙整」。
  * 反例(消費資訊彙整):信用卡多筆消費彙整 — 一封信包含多則
    刷卡紀錄的彙總 — 也是「消費資訊彙整」,不是「消費支出」。

### 4. 急迫性 (urgency)

very_urgent  = 非常緊急,建議立即處理
urgent       = 緊急,建議 3 小時內處理
normal       = 普通,建議一天到一周內處理
none         = 沒有急迫性

### 5. 關鍵字 (keywords, 至多 5 個)

從信件內容截取至多 5 個關鍵字,每個 2-8 字,
能代表信件主題或重要實體。

只輸出結構化結果,不要任何說明文字。
"""


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
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

        # Two model instances against the same server:
        # - analyze: capped by LLM_ANALYZE_MAX_TOKENS. Bounds
        #   degenerate JSON loops so LengthFinishReasonError fires in
        #   ~1s instead of after the whole context window (~30s).
        # - expense extraction: uncapped. The extract JSON can be
        #   larger (multiple items, attachments), and its failure mode
        #   is not the same degenerate-loop risk.
        llm_settings = LLMSettings()
        analyze_chat_model = get_chat_model(
            llm_settings, max_tokens=llm_settings.analyze_max_tokens
        )
        extract_chat_model = get_chat_model(llm_settings)
        self._analyze_model = analyze_chat_model.with_structured_output(
            EmailAnalysis
        )
        self._expense_subgraph = ExpenseSubgraph(
            checkpointer=checkpointer,
            session_factory=session_factory,
            client=client,
            model=extract_chat_model,
            notion=notion,
        )
        self._graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver):
        builder = StateGraph(AgentState)
        builder.add_node("analyze", self._analyze)
        builder.add_node("expense_sg", self._expense_subgraph.graph)
        builder.add_node("notify", self._notify)

        builder.add_edge(START, "analyze")
        builder.add_conditional_edges(
            "analyze",
            self._route_by_category,
            {"expense": "expense_sg", "notify": "notify"},
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
        with node_trace(log, "analyze"):
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
                with record_call(
                    counter=llm_calls_total,
                    histogram=llm_latency_seconds,
                    counter_labels={"node": "analyze"},
                    histogram_labels={"node": "analyze"},
                ):
                    analysis = self._analyze_model.invoke(
                        [
                            SystemMessage(content=ANALYZE_SYSTEM_PROMPT),
                            HumanMessage(content=user_text),
                        ]
                    )
            except LengthFinishReasonError as exc:
                usage = exc.completion.usage
                detail = (
                    f"prompt={usage.prompt_tokens} "
                    f"completion={usage.completion_tokens}"
                )
                log.warning(f"analyze: LLM hit token limit ({detail})")
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
                return {
                    "analysis": None,
                    "side_effect": AnalysisFailed(
                        reason="prompt_too_long", detail=detail
                    ),
                }
            if analysis is not None:
                log.info(f"classified as {analysis.category}")
            return {"analysis": analysis}

    # Categories that go through the expense subgraph — where PDF /
    # HTML attachments are pulled and the LLM extracts structured
    # payment fields (amount, vendor, transacted_at, …). Kept as a
    # tuple so a future addition (e.g. 訂閱服務 for recurring charges)
    # is a one-line change.
    _EXPENSE_LIKE_CATEGORIES = ("消費支出", "帳單通知")

    def _route_by_category(self, state: AgentState) -> str:
        log = self._log(state)
        analysis = state.get("analysis")
        if analysis is None:
            log.info("routing to notify (no analysis)")
            return "notify"
        target = (
            "expense"
            if analysis.category in self._EXPENSE_LIKE_CATEGORIES
            else "notify"
        )
        log.info(f"routing to {target} (category={analysis.category})")
        return target

    def _notify(self, state: AgentState) -> dict:
        log = self._log(state)
        with node_trace(log, "notify"):
            analysis = state["analysis"]
            side_effect = state.get("side_effect")
            if analysis is None:
                # Analyze itself failed — no structured summary to
                # render. If it left us an AnalysisFailed marker we
                # still ping the user (they need to open the mail
                # manually); otherwise we log and skip since something
                # else went off-script.
                if isinstance(side_effect, AnalysisFailed):
                    text = _format_analysis_failed(
                        state["email"], side_effect
                    )
                    self._notifier.send_message(text)
                    log.info("notified user of analyze failure")
                    return {}
                log.warning("notify: skipping — no analysis")
                return {}
            text = _format_message(state["email"], analysis, side_effect)
            self._notifier.send_message(text)
            log.info("notified user")
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
