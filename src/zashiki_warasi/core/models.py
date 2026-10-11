"""SQLAlchemy ORM models."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Float,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
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
    # Vocabulary `category` belongs to. The app always writes it; the
    # server default only covers pre-v2 code still writing during a
    # rolling deploy (and image rollback), which is v1 by definition.
    taxonomy_version: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="v1",
    )
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
    # Vocabulary of `llm_category` / `laya_category` (see EmailAnalysis).
    taxonomy_version: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="v1",
    )
    predicted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class EmailClassification(Base):
    """Classification history — transaction-time SCD type 2.

    One row per version of our judgement about a message. Validity is
    processing time, half-open `[valid_from, valid_to)`; the current
    row is the one with `valid_to IS NULL` (no separate flag that could
    drift). Writes go through `ClassificationWriter`, never ad hoc.

    Postgres additionally enforces non-overlapping ranges per message
    with an EXCLUDE constraint (btree_gist) created in migration 0009;
    it is not declared here so SQLite-based tests can `create_all`.
    """

    __tablename__ = "email_classifications"
    __table_args__ = (
        CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_email_classifications_valid_range",
        ),
        CheckConstraint(
            "source IN ('llm', 'laya', 'human')",
            name="ck_email_classifications_source",
        ),
        CheckConstraint(
            "recorded_by IN ('app', 'migration', 'import')",
            name="ck_email_classifications_recorded_by",
        ),
        Index(
            "uq_email_classifications_current",
            "message_id",
            unique=True,
            postgresql_where=text("valid_to IS NULL"),
            sqlite_where=text("valid_to IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    message_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("email_analyses.message_id"),
        nullable=False,
    )
    category: Mapped[str] = mapped_column(String(32), nullable=False)
    taxonomy_version: Mapped[str] = mapped_column(String(8), nullable=False)
    source: Mapped[str] = mapped_column(String(8), nullable=False)
    recorded_by: Mapped[str] = mapped_column(String(16), nullable=False)
    valid_from: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    valid_to: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class TaxonomyMapping(Base):
    """Append-only cross-version category map.

    The primary key makes each (rev, from_version, from_category,
    to_version) map to exactly one target. Rows are never updated or
    deleted — a correction adds a new `mapping_rev`, and readers (the
    `email_category_current` view) pin a rev.
    """

    __tablename__ = "taxonomy_mappings"

    mapping_rev: Mapped[int] = mapped_column(Integer, primary_key=True)
    from_version: Mapped[str] = mapped_column(String(8), primary_key=True)
    from_category: Mapped[str] = mapped_column(String(32), primary_key=True)
    to_version: Mapped[str] = mapped_column(String(8), primary_key=True)
    to_category: Mapped[str] = mapped_column(String(32), nullable=False)
    is_exact: Mapped[bool] = mapped_column(Boolean, nullable=False)
