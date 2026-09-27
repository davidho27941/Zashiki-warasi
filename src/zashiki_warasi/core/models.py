"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class GmailSyncState(Base):
    __tablename__ = "gmail_sync_state"

    email_address: Mapped[str] = mapped_column(String(320), primary_key=True)
    history_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class EmailAnalysis(Base):
    __tablename__ = "email_analyses"

    message_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    importance: Mapped[int] = mapped_column(Integer, nullable=False)
    urgency: Mapped[str] = mapped_column(String(16), nullable=False)
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    keywords: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=list,
    )
    analyzed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class ExpenseRecord(Base):
    __tablename__ = "expenses"

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    message_id: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False,
    )

    title: Mapped[str | None] = mapped_column(String(128), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), nullable=True,
    )
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    transacted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    vendor: Mapped[str | None] = mapped_column(String(256), nullable=True)
    location: Mapped[str | None] = mapped_column(String(512), nullable=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True)
    transaction_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    payment_method: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )

    # Full ExpenseDraft JSON for audit / debugging without re-LLM-call.
    raw_extraction: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Notion mirror — set after a successful sync; mutually exclusive
    # with notion_sync_error in normal operation.
    notion_page_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    notion_sync_error: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    # Last time the puller pulled this row back from Notion. NULL means
    # the row has never been reverse-synced (created from email only).
    notion_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class NotionSyncState(Base):
    """Polling cursor for `NotionExpensePuller`. Keyed by database_id
    so a future multi-DB setup needs no schema change."""

    __tablename__ = "notion_sync_state"

    database_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LayaShadowPrediction(Base):
    """One shadow-classifier prediction per (email, model version).

    v1.6.0 `add-laya-shadow-classifier`. The LLM stays authoritative;
    these rows are observational — agreement measurement, audit input
    (issue #6 CLI), and future fine-tune training pairs. `input_text`
    is the load-bearing column: it snapshots the EXACT truncated text
    sent to laya (see `classifier/truncation.py`), because the
    `email_analyses` table stores no email text and rows here must be
    self-contained `(input, prediction)` samples.

    `laya_model_ver` is derived (`{semantic}-{sha1(questions_dict)[:8]}`)
    so any questions-dict edit automatically starts a fresh comparison
    series under the (message_id, laya_model_ver) uniqueness — old rows
    stay queryable for cross-version analysis.

    `laya_alternates` is JSONB on Postgres (prod + PG-backed tests) and
    plain JSON elsewhere so the SQLite-based agent test harness can
    still `create_all` the full metadata.
    """

    __tablename__ = "laya_shadow_predictions"
    __table_args__ = (
        UniqueConstraint(
            "message_id", "laya_model_ver",
            name="uq_laya_shadow_message_model",
        ),
        Index("ix_laya_shadow_llm_category", "llm_category"),
        Index("ix_laya_shadow_laya_category", "laya_category"),
        Index("ix_laya_shadow_predicted_at", "predicted_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    input_text: Mapped[str] = mapped_column(Text, nullable=False)
    llm_category: Mapped[str] = mapped_column(String(32), nullable=False)
    laya_category: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    laya_confidence: Mapped[float | None] = mapped_column(
        Float, nullable=True,
    )
    laya_alternates: Mapped[list | None] = mapped_column(
        JSON().with_variant(JSONB(), "postgresql"), nullable=True,
    )
    laya_latency_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    laya_error: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    laya_model_ver: Mapped[str] = mapped_column(String(64), nullable=False)
    predicted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
