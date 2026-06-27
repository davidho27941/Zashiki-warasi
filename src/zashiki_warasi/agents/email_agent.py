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
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.llm import get_chat_model
from zashiki_warasi.agents.verticals.expense import ExpenseSubgraph
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import (
    EmailAnalysis,
    EmailMessage,
    ExpenseLogged,
    ExpenseNeedsReview,
    SideEffect,
    coerce_importance,
)
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.notifications.telegram import TelegramNotifier

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

以 5W1H (誰、什麼、何時、何地、為何、如何) 為核心做高層次摘要,
只回答「這封信為什麼存在、誰寄的、要做什麼」這類整體資訊。

**不要在摘要中提及具體金額、商家名稱、交易時間、訂單編號**
等細節 — 這些由結構化的支付資訊欄位處理。摘要與結構化欄位
重疊只會增加不一致風險。

例:
- 好:「Amazon 日本訂單確認通知,商品已成立訂單等候出貨」
- 不好:「於 Amazon 訂購商品,共 3,200 日圓,訂單編號 250-1234567」

### 3. 分類 (category)

從以下列表選擇 **一項**:
消費支出、訂閱服務、技術文章、講座資訊、會議邀請、帳單通知、
廣告、促銷、社交、新聞、安全通知、股票資訊、其他

特例:金融產品(基金、ETF、信貸、信用卡帳單分期等)的「推銷」郵件
一律歸類為「廣告」或「促銷」,不要分到「消費支出」或「帳單通知」。

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
    ) -> None:
        self._session_factory = session_factory
        self._notifier = notifier

        chat_model = get_chat_model()
        self._analyze_model = chat_model.with_structured_output(EmailAnalysis)
        self._expense_subgraph = ExpenseSubgraph(
            checkpointer=checkpointer,
            session_factory=session_factory,
            client=client,
            model=chat_model,
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

    def _analyze(self, state: AgentState) -> dict:
        email = state["email"]
        user_text = (
            f"From: {email.from_address}\n"
            f"Subject: {email.subject}\n"
            f"Date: {email.received_at.isoformat()}\n"
            f"\n"
            f"{email.body_plain or email.snippet}"
        )
        analysis = self._analyze_model.invoke(
            [
                SystemMessage(content=ANALYZE_SYSTEM_PROMPT),
                HumanMessage(content=user_text),
            ]
        )
        return {"analysis": analysis}

    def _route_by_category(self, state: AgentState) -> str:
        analysis = state.get("analysis")
        if analysis is None:
            return "notify"
        if analysis.category == "消費支出":
            return "expense"
        return "notify"

    def _notify(self, state: AgentState) -> dict:
        analysis = state["analysis"]
        if analysis is None:
            logger.warning(
                f"notify: skipping {state['email'].id} — no analysis"
            )
            return {}
        text = _format_message(
            state["email"], analysis, state.get("side_effect")
        )
        self._notifier.send_message(text)
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
    lines = ["💰 <b>已記帳</b>"]

    if effect.amount is not None:
        amt = f"{effect.amount} {effect.currency or ''}".strip()
    else:
        amt = "不明"
    lines.append(f"  金額: {html.escape(amt)}")

    lines.append(f"  商家: {html.escape(effect.vendor or '不明')}")

    if effect.transacted_at:
        lines.append(f"  時間: {effect.transacted_at:%Y-%m-%d %H:%M}")

    if effect.payment_method:
        if effect.payment_method == "其他":
            lines.append("  支付: ⚠️ 其他 (請檢查信件確認)")
        else:
            lines.append(f"  支付: {html.escape(effect.payment_method)}")

    if effect.transaction_id:
        lines.append(
            f"  編號: <code>{html.escape(effect.transaction_id)}</code>"
        )

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
