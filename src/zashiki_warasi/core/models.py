"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Integer,
    Numeric,
    String,
    Text,
    Uuid,
    func,
)
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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
