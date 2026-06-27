"""Expense vertical: extract structured payment info, persist, set SideEffect."""

from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
from typing import TypedDict

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from zashiki_warasi.agents.verticals.pdf import collect_text
from zashiki_warasi.core.models import ExpenseRecord
from zashiki_warasi.core.schemas import (
    EmailAnalysis,
    EmailMessage,
    ExpenseDraft,
    ExpenseLogged,
    ExpenseNeedsReview,
    SideEffect,
)
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.notifications.notion import (
    NotionExpenseRecorder,
    NotionSyncError,
)

logger = logging.getLogger(__name__)


AUTO_TRANSACTION_ID_PREFIX = "AUTO-"


DEDUP_WINDOW_MINUTES = 15


def find_duplicate(
    draft: ExpenseDraft,
    session: Session,
    window_minutes: int = DEDUP_WINDOW_MINUTES,
) -> ExpenseRecord | None:
    """Locate an existing ExpenseRecord that matches `draft`.

    Two-stage match:

    1. Real `transaction_id` collision (the email's order/receipt
       number is identical to one we already stored). Strongest
       signal; immediately conclusive.
    2. Amount + currency + a narrow time window around
       `transacted_at`. Catches the cross-system case where a single
       transaction generates two emails with different identifiers —
       e.g. SMBC Olive's `承認番号` plus Starbucks's order receipt
       for the same purchase, arriving seconds apart with the same
       amount.

    Returns None when no decisive match is found. Multiple Stage-2
    candidates within the window is treated as "ambiguous, accept as
    new" rather than risk merging into the wrong record.
    """
    # Stage 1: real transaction_id match. AUTO- ids are per-email
    # deterministic so they can never coincide across distinct emails;
    # we still skip them here to make the intent explicit.
    if (
        draft.transaction_id
        and not draft.transaction_id.startswith(AUTO_TRANSACTION_ID_PREFIX)
    ):
        match = session.scalar(
            select(ExpenseRecord).where(
                ExpenseRecord.transaction_id == draft.transaction_id,
            )
        )
        if match is not None:
            return match

    # Stage 2: (amount, currency, transacted_at ± window). Skip if any
    # of the three required signals is missing — we'd be guessing.
    if (
        draft.amount is not None
        and draft.currency
        and draft.transacted_at is not None
    ):
        delta = timedelta(minutes=window_minutes)
        lo = draft.transacted_at - delta
        hi = draft.transacted_at + delta
        candidates = list(
            session.scalars(
                select(ExpenseRecord).where(
                    ExpenseRecord.amount == draft.amount,
                    ExpenseRecord.currency == draft.currency,
                    ExpenseRecord.transacted_at.between(lo, hi),
                )
            ).all()
        )
        if len(candidates) == 1:
            return candidates[0]
        # 0 -> definitely new; >1 -> ambiguous, do not merge

    return None


def auto_transaction_id(message_id: str) -> str:
    """Deterministic stand-in for transaction_id when the email itself
    carries no order/receipt number.

    Derived from a SHA256 of the Gmail message ID so a retry / LangGraph
    resume always produces the same value (no risk of a second
    persist attempt picking a different id and looking like a new
    expense). The `AUTO-` prefix lets the user distinguish at a glance
    between numbers we read from the email and numbers we made up.
    """
    digest = hashlib.sha256(message_id.encode("utf-8")).hexdigest()[:12]
    return f"{AUTO_TRANSACTION_ID_PREFIX}{digest}"


EXPENSE_EXTRACT_SYSTEM_PROMPT = """\
你是消費支出資訊擷取助理。閱讀使用者提供的電子郵件(可能含 PDF
附件文字),找出與「消費支出」相關的結構化欄位並輸出。

規則:

1. 信件可能是繁體中文、簡體中文、英文或日文。

2. 任何欄位若信件未明確提及,請回傳 null,不要猜測或編造。
   唯一例外:幣別可從金額符號或上下文推斷
   (¥/円→JPY、NT$/新台幣→TWD、$/USD→USD)。

3. transacted_at 用 ISO 8601 (YYYY-MM-DD HH:MM:SS):
   - 信件有完整日期時間→直接用
   - 只有日期沒有時間→時間部分用 00:00:00
   - 連日期都沒有→回傳 null

4. payment_method 的判斷邏輯:
   - 信件「沒提到」支付方式 → null
   - 信件提到支付方式,且符合以下品牌之一 → 填對應字串:
     · Rakuten Pay
     · SMBC Olive (含「SMBC Oliveフレキシブルペイ」等變體)
     · 三菱UFJ-JCB (含「三菱UFJデビット」等變體)
     · PayPay
   - 信件提到「信用卡」、「クレジットカード」、「credit card」等
     一般信用卡敘述,但品牌不屬於上述四種 → "信用卡"
   - 信件提到「現金」、「cash」、「代引き」(貨到付款的現金部分)
     → "現金"
   - 提到了支付方式但都不屬於以上類別 (e.g. LINE Pay、銀行轉帳、
     コンビニ後払い) → "其他"

5. 「伝票番号」、「注文番号」、「order id」、「transaction id」、
   「訂單編號」等都歸 transaction_id。

6. vendor 是消費場所/商家 (e.g. ファミリーマート、Amazon);
   location 是物理地址 (e.g. 東京都渋谷区)。兩者可同時存在,
   通常 vendor 必有、location 常缺。

7. category 用簡短中文標籤 (e.g. 餐飲、交通、購物、訂閱、水電)。

8. 反幻想規則(極重要):
   - 若你看到的內容只是模糊提示而非具體支出資料
     (e.g.「您有新訂單,詳見附件」但附件本身未附在以下提供的文字中)
     → 所有欄位回 null。
   - 不要根據寄件者 (e.g. Amazon)、主旨關鍵字或一般常識去猜測
     金額、商家、時間。
   - 若你不確定某個欄位的值,寧可回 null 也不要編造。
   - 只有當該欄位的資訊「明確出現在以下提供的信件或附件文字中」
     才填寫。

只輸出結構化結果,不要任何說明文字。
"""


class ExpenseState(TypedDict):
    """Subgraph state. Overlapping fields with parent AgentState
    (email, analysis, side_effect) are merged back when the subgraph
    exits; `extracted` stays internal to the subgraph."""

    email: EmailMessage
    analysis: EmailAnalysis | None
    side_effect: SideEffect | None
    extracted: ExpenseDraft | None


class ExpenseSubgraph:
    """Expense vertical packaged as a class for symmetry with EmailAgent.

    Builds its own compiled StateGraph in `__init__`; consumers wire
    `.graph` into the parent graph via `add_node("expense_sg", sg.graph)`.
    """

    def __init__(
        self,
        *,
        checkpointer: PostgresSaver | None,
        session_factory: sessionmaker,
        client: GmailClient,
        model: BaseChatModel,
        notion: NotionExpenseRecorder | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._client = client
        self._notion = notion
        self._structured_model = model.with_structured_output(ExpenseDraft)
        self.graph = self._build_graph(checkpointer)

    def _build_graph(self, checkpointer: PostgresSaver | None):
        builder = StateGraph(ExpenseState)
        builder.add_node("extract", self._extract_node)
        builder.add_node("persist", self._persist_node)
        builder.add_edge(START, "extract")
        builder.add_edge("extract", "persist")
        builder.add_edge("persist", END)
        return builder.compile(checkpointer=checkpointer)

    # ----- nodes -----

    def _extract_node(self, state: ExpenseState) -> dict:
        email = state["email"]
        text, unreadable_pdfs = collect_text(email, self._client)

        # Early bail: PDF present but unreadable AND no body text — no
        # signal to extract from; do not hallucinate.
        if not text and unreadable_pdfs:
            logger.info(
                f"expense: {email.id} has only unreadable PDFs "
                "→ needs_review"
            )
            return {
                "side_effect": ExpenseNeedsReview(
                    reason="image_pdf_unreadable",
                    unreadable_attachments=unreadable_pdfs,
                ),
                "extracted": None,
            }

        if not text:
            return {
                "side_effect": ExpenseNeedsReview(
                    reason="extraction_yielded_nulls",
                ),
                "extracted": None,
            }

        user_prompt = self._build_user_prompt(email, text)
        draft: ExpenseDraft = self._structured_model.invoke(
            [
                SystemMessage(content=EXPENSE_EXTRACT_SYSTEM_PROMPT),
                HumanMessage(content=user_prompt),
            ]
        )
        return {"extracted": draft}

    def _persist_node(self, state: ExpenseState) -> dict:
        # If extract already set a side_effect (needs_review), nothing
        # to persist — pass through.
        if state.get("side_effect") is not None:
            return {}

        draft = state.get("extracted")
        if draft is None or (draft.amount is None and draft.vendor is None):
            logger.info(
                "expense: LLM extraction too sparse (no amount & no vendor) "
                "→ needs_review"
            )
            return {
                "side_effect": ExpenseNeedsReview(
                    reason="extraction_yielded_nulls",
                )
            }

        transaction_id = draft.transaction_id or auto_transaction_id(
            state["email"].id
        )

        with self._session_factory() as session:
            # Cross-email dedup: detect "different email, same actual
            # transaction" (e.g. SMBC Olive confirmation + Starbucks
            # merchant receipt) before writing a second row.
            existing = find_duplicate(draft, session)
            is_new_record = existing is None
            if existing is not None:
                logger.info(
                    f"expense: {state['email'].id} matches existing record "
                    f"{existing.id} (duplicate transaction) → skip persist"
                )
                record = existing
            else:
                record = ExpenseRecord(
                    message_id=state["email"].id,
                    amount=draft.amount,
                    currency=draft.currency,
                    transacted_at=draft.transacted_at,
                    vendor=draft.vendor,
                    location=draft.location,
                    category=draft.category,
                    transaction_id=transaction_id,
                    payment_method=draft.payment_method,
                    raw_extraction=draft.model_dump(mode="json"),
                )
                session.add(record)
                try:
                    session.commit()
                except IntegrityError:
                    # message_id UNIQUE collided — already persisted on
                    # a prior tick (LangGraph resume scenario, NOT
                    # cross-email dedup which is handled above). Use
                    # the existing row for the SideEffect.
                    session.rollback()
                    record = session.scalar(
                        select(ExpenseRecord).where(
                            ExpenseRecord.message_id == state["email"].id
                        )
                    )
                    is_new_record = False

            # Best-effort Notion sync. Only attempted for newly-written
            # records — duplicates re-use the existing row's
            # notion_page_id / notion_sync_error from the original
            # write, no re-attempt here.
            if (
                self._notion is not None
                and is_new_record
                and record.notion_page_id is None
                and record.notion_sync_error is None
            ):
                try:
                    record.notion_page_id = self._notion.record_expense(record)
                    logger.info(
                        f"expense: synced {record.id} to Notion page "
                        f"{record.notion_page_id}"
                    )
                except NotionSyncError as exc:
                    err = str(exc)[:500]  # keep DB row size sane
                    record.notion_sync_error = err
                    logger.warning(
                        f"expense: Notion sync failed for {record.id}: {err}"
                    )
                # record is attached to the open session; commit flushes
                # whichever of notion_page_id / notion_sync_error we set.
                session.commit()

        return {
            "side_effect": ExpenseLogged(
                record_id=str(record.id),
                amount=record.amount,
                currency=record.currency,  # type: ignore[arg-type]
                vendor=record.vendor,
                location=record.location,
                category=record.category,
                transacted_at=record.transacted_at,
                payment_method=record.payment_method,  # type: ignore[arg-type]
                transaction_id=record.transaction_id,
                notion_page_id=record.notion_page_id,
                notion_sync_error=record.notion_sync_error,
            )
        }

    # ----- prompt construction -----

    @staticmethod
    def _build_user_prompt(email: EmailMessage, combined_text: str) -> str:
        return (
            f"寄件者: {email.from_address}\n"
            f"主旨: {email.subject}\n"
            f"收件時間: {email.received_at.isoformat()}\n"
            f"\n"
            f"信件內容 (含 PDF 附件抽出文字,若有):\n"
            f"{combined_text}"
        )
