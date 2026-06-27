"""Shared data models passed between Gmail layer and agents."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


# ----- Gmail layer -----


class AttachmentMeta(BaseModel):
    """Metadata for an attachment; bytes fetched on demand via GmailClient."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    filename: str
    mime_type: str
    size: int


class EmailMessage(BaseModel):
    """Parsed Gmail message ready for agent consumption."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str
    history_id: int
    from_address: str
    to_addresses: list[str] = Field(default_factory=list)
    cc_addresses: list[str] = Field(default_factory=list)
    subject: str = ""
    snippet: str = ""
    body_plain: str | None = None
    body_html: str | None = None
    received_at: datetime
    labels: list[str] = Field(default_factory=list)
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    raw_headers: dict[str, str] = Field(default_factory=dict)


class ProfileInfo(BaseModel):
    """Gmail account profile used to baseline history polling."""

    model_config = ConfigDict(frozen=True)

    email: str
    history_id: int


# ----- Analysis -----


Urgency = Literal["very_urgent", "urgent", "normal", "none"]

Category = Literal[
    "消費支出",
    "訂閱服務",
    "技術文章",
    "講座資訊",
    "會議邀請",
    "帳單通知",
    "廣告",
    "促銷",
    "社交",
    "新聞",
    "安全通知",
    "股票資訊",
    "其他",
]


class EmailAnalysis(BaseModel):
    """High-level analysis produced by the analyze node.

    Importance / urgency / category / summary / keywords come from a
    single LLM call. Payment-specific details (amount, vendor, etc.)
    are handled by the expense subgraph and surface via SideEffect —
    they are intentionally NOT duplicated in `summary` to avoid
    inconsistency between the two extraction paths.
    """

    model_config = ConfigDict(frozen=True)

    importance: int = Field(
        ..., ge=1, le=5,
        description="重要度 1(非常不重要) 到 5(非常重要)",
    )
    urgency: Urgency = Field(description="急迫性等級")
    category: Category = Field(description="內容分類")
    summary: str = Field(
        ...,
        description="50-200 字以內的 5W1H 高層次摘要,不含具體支付資訊",
    )
    keywords: list[str] = Field(
        default_factory=list,
        max_length=5,
        description="至多 5 個關鍵字",
    )


# ----- Expense vertical -----


Currency = Literal["JPY", "TWD", "USD"]


PaymentMethod = Literal[
    "Rakuten Pay",
    "SMBC Olive",
    "三菱UFJ-JCB",
    "PayPay",
    "信用卡",
    "現金",
    "其他",
]


class ExpenseDraft(BaseModel):
    """LLM extraction of payment info from a single email + its PDF
    attachments. All fields nullable — the LLM must use null when the
    source text does not mention that detail rather than fabricate it.
    """

    model_config = ConfigDict(frozen=True)

    amount: Decimal | None = Field(
        default=None,
        description="決済金額。信件未提及請回傳 null。",
    )
    currency: Currency | None = Field(
        default=None,
        description=(
            "幣別。可從金額符號或上下文推斷 "
            "(¥/円→JPY、NT$/新台幣→TWD、$/USD→USD)。"
        ),
    )
    transacted_at: datetime | None = Field(
        default=None,
        description=(
            "消費時間,ISO 8601 (YYYY-MM-DD HH:MM:SS)。"
            "只有日期無時間時時間用 00:00:00;連日期都無則 null。"
        ),
    )
    vendor: str | None = Field(
        default=None,
        description="商家名稱 (e.g. ファミリーマート、Amazon)。",
    )
    location: str | None = Field(
        default=None,
        description="消費地點的物理地址 (e.g. 東京都渋谷区)。",
    )
    category: str | None = Field(
        default=None,
        description="消費類別簡短中文標籤 (e.g. 餐飲、交通、購物、訂閱、水電)。",
    )
    transaction_id: str | None = Field(
        default=None,
        description="交易識別碼:伝票番号、注文番号、order ID 等都歸這一欄。",
    )
    payment_method: PaymentMethod | None = Field(
        default=None,
        description=(
            "支付方式。null = 信件未提及;'其他' = 提及但不在白名單。"
        ),
    )


class ExpenseLogged(BaseModel):
    """SideEffect payload when an ExpenseRecord was successfully written."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["expense"] = "expense"
    record_id: str  # uuid stringified

    amount: Decimal | None
    currency: Currency | None
    vendor: str | None
    transacted_at: datetime | None
    payment_method: PaymentMethod | None
    transaction_id: str | None


class ExpenseNeedsReview(BaseModel):
    """SideEffect payload when expense extraction couldn't proceed.

    Not persisted to the expenses table — that table is only for
    confirmed records. Notify surfaces this so the user can handle
    the email manually.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["expense_needs_review"] = "expense_needs_review"
    reason: Literal[
        "image_pdf_unreadable",
        "extraction_yielded_nulls",
    ]
    unreadable_attachments: list[str] = Field(default_factory=list)


SideEffect = Annotated[
    ExpenseLogged | ExpenseNeedsReview,
    Field(discriminator="kind"),
]
