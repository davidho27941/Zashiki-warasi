"""Expense subgraph: extract node, persist node, end-to-end."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.verticals.expense import ExpenseSubgraph
from zashiki_warasi.core.models import Base, ExpenseRecord
from zashiki_warasi.core.schemas import (
    AttachmentMeta,
    EmailAnalysis,
    EmailMessage,
    ExpenseDraft,
    ExpenseLogged,
    ExpenseNeedsReview,
)


# ---------- fixtures ----------


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def fake_email() -> EmailMessage:
    return EmailMessage(
        id="msg-exp-1",
        thread_id="t",
        history_id=100,
        from_address="auto-confirm@amazon.co.jp",
        subject="Amazon.co.jp ご注文の確認",
        body_plain="ご注文ありがとうございます。注文番号 250-1234567。\n合計: ¥3,200",
        received_at=datetime(2026, 6, 27, 14, 32, tzinfo=timezone.utc),
        attachments=[],
    )


@pytest.fixture
def fake_analysis() -> EmailAnalysis:
    return EmailAnalysis(
        importance=3,
        urgency="none",
        category="消費支出",
        summary="Amazon 訂單確認",
        keywords=["Amazon", "訂單"],
    )


@pytest.fixture
def mock_client() -> MagicMock:
    return MagicMock(name="gmail_client")


def _build_model_returning(draft: ExpenseDraft | None) -> MagicMock:
    """Helper to build a chat-model mock whose structured runnable
    returns the given draft when invoked."""
    structured = MagicMock(name="structured")
    structured.invoke.return_value = draft
    model = MagicMock(name="chat_model")
    model.with_structured_output.return_value = structured
    return model


def _draft(**overrides) -> ExpenseDraft:
    base = dict(
        amount=Decimal("3200"),
        currency="JPY",
        transacted_at=datetime(2026, 6, 27, 14, 32, tzinfo=timezone.utc),
        vendor="Amazon.co.jp",
        location=None,
        category="購物",
        transaction_id="250-1234567",
        payment_method="SMBC Olive",
    )
    base.update(overrides)
    return ExpenseDraft(**base)


def _initial_state(email, analysis) -> dict:
    return {
        "email": email,
        "analysis": analysis,
        "side_effect": None,
        "extracted": None,
    }


# ---------- happy path ----------


class TestHappyPath:
    def test_extracts_and_persists(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft()
        model = _build_model_returning(draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )
        config = {"configurable": {"thread_id": fake_email.id}}

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config=config,
        )

        # side_effect is an ExpenseLogged
        se = result["side_effect"]
        assert isinstance(se, ExpenseLogged)
        assert se.kind == "expense"
        assert se.amount == Decimal("3200")
        assert se.currency == "JPY"
        assert se.vendor == "Amazon.co.jp"
        assert se.payment_method == "SMBC Olive"
        assert se.transaction_id == "250-1234567"

        # ExpenseRecord written to DB
        with session_factory() as session:
            count = session.scalar(
                select(func.count()).select_from(ExpenseRecord)
            )
            assert count == 1
            row = session.scalar(
                select(ExpenseRecord).where(
                    ExpenseRecord.message_id == fake_email.id
                )
            )
            assert row is not None
            assert row.amount == Decimal("3200")
            assert row.vendor == "Amazon.co.jp"
            assert row.raw_extraction["payment_method"] == "SMBC Olive"


# ---------- auto transaction id ----------


class TestAutoTransactionId:
    def test_uses_llm_extracted_when_present(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft(transaction_id="REAL-FROM-EMAIL-001")
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=_build_model_returning(draft),
        )

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        assert result["side_effect"].transaction_id == "REAL-FROM-EMAIL-001"

    def test_generates_auto_id_when_missing(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft(transaction_id=None)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=_build_model_returning(draft),
        )

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        tid = result["side_effect"].transaction_id
        assert tid is not None
        assert tid.startswith("AUTO-")
        assert len(tid) == 17  # "AUTO-" + 12 hex chars

    def test_auto_id_is_deterministic_for_same_email(self):
        """Crash-resume safety: two persist attempts on the same email
        must yield the same auto-id (not a random value each time)."""
        from zashiki_warasi.agents.verticals.expense import (
            auto_transaction_id,
        )

        assert auto_transaction_id("msg-abc") == auto_transaction_id(
            "msg-abc"
        )
        assert auto_transaction_id("msg-abc") != auto_transaction_id(
            "msg-xyz"
        )

    def test_auto_id_persisted_to_db(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft(transaction_id=None)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=_build_model_returning(draft),
        )

        subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        with session_factory() as session:
            row = session.scalar(
                select(ExpenseRecord).where(
                    ExpenseRecord.message_id == fake_email.id
                )
            )
            assert row.transaction_id.startswith("AUTO-")


# ---------- early bail: image PDF only ----------


class TestImagePdfFallback:
    def test_unreadable_pdf_with_empty_body_skips_llm(
        self, session_factory, mock_client, fake_analysis, monkeypatch
    ):
        att = AttachmentMeta(
            attachment_id="a",
            filename="scan.pdf",
            mime_type="application/pdf",
            size=1000,
        )
        email = EmailMessage(
            id="msg-img",
            thread_id="t",
            history_id=1,
            from_address="x@y.com",
            subject="receipt",
            body_plain=None,
            snippet="",
            received_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
            attachments=[att],
        )
        mock_client.get_attachment.return_value = b"image-bytes"
        # Force the PDF extractor to return empty (image-only PDF).
        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            lambda _b: "",
        )

        # LLM should NOT be invoked.
        structured = MagicMock(name="structured")
        model = MagicMock(name="chat_model")
        model.with_structured_output.return_value = structured

        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )
        result = subgraph.graph.invoke(
            _initial_state(email, fake_analysis),
            config={"configurable": {"thread_id": email.id}},
        )

        # LLM was not called
        structured.invoke.assert_not_called()
        # SideEffect is needs_review
        se = result["side_effect"]
        assert isinstance(se, ExpenseNeedsReview)
        assert se.reason == "image_pdf_unreadable"
        assert se.unreadable_attachments == ["scan.pdf"]
        # Nothing persisted
        with session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(ExpenseRecord)
            ) == 0


# ---------- persist node: null fallback ----------


class TestNullFallback:
    def test_draft_with_all_nulls_routed_to_needs_review(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        # LLM returns a draft with neither amount nor vendor.
        empty_draft = ExpenseDraft()  # all None
        model = _build_model_returning(empty_draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        se = result["side_effect"]
        assert isinstance(se, ExpenseNeedsReview)
        assert se.reason == "extraction_yielded_nulls"
        with session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(ExpenseRecord)
            ) == 0

    def test_amount_only_is_enough_to_persist(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft(vendor=None, location=None, transaction_id=None)
        model = _build_model_returning(draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        assert isinstance(result["side_effect"], ExpenseLogged)

    def test_vendor_only_is_enough_to_persist(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft(amount=None, currency=None, transaction_id=None)
        model = _build_model_returning(draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        assert isinstance(result["side_effect"], ExpenseLogged)


# ---------- idempotency at LangGraph layer ----------


class TestIdempotency:
    def test_unique_constraint_on_second_persist_returns_existing(
        self, session_factory, mock_client, fake_email, fake_analysis
    ):
        draft = _draft()
        model = _build_model_returning(draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )
        # Pre-seed an existing row to force the UNIQUE collision branch.
        with session_factory() as session:
            existing = ExpenseRecord(
                message_id=fake_email.id,
                amount=Decimal("999"),
                currency="JPY",
                transacted_at=None,
                vendor="pre-existing",
                location=None,
                category=None,
                transaction_id=None,
                payment_method=None,
                raw_extraction={},
            )
            session.add(existing)
            session.commit()
            existing_id = str(existing.id)

        result = subgraph.graph.invoke(
            _initial_state(fake_email, fake_analysis),
            config={"configurable": {"thread_id": fake_email.id}},
        )

        # SideEffect references the pre-existing row, not a new one.
        se = result["side_effect"]
        assert isinstance(se, ExpenseLogged)
        assert se.record_id == existing_id
        assert se.vendor == "pre-existing"

        # Still only one row.
        with session_factory() as session:
            assert session.scalar(
                select(func.count()).select_from(ExpenseRecord)
            ) == 1


# ---------- system prompt ----------


class TestExtractPrompt:
    def test_user_prompt_includes_body_and_pdf_text(
        self, session_factory, mock_client, fake_email, fake_analysis,
        monkeypatch,
    ):
        att = AttachmentMeta(
            attachment_id="a",
            filename="r.pdf",
            mime_type="application/pdf",
            size=1,
        )
        email = fake_email.model_copy(update={"attachments": [att]})
        mock_client.get_attachment.return_value = b"pdf"
        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            lambda _b: "PDF EXTRACTED TEXT 12345",
        )

        draft = _draft()
        model = _build_model_returning(draft)
        subgraph = ExpenseSubgraph(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            client=mock_client,
            model=model,
        )

        subgraph.graph.invoke(
            _initial_state(email, fake_analysis),
            config={"configurable": {"thread_id": email.id}},
        )

        # Verify the LLM saw both body and PDF text in the user message.
        structured = model.with_structured_output.return_value
        messages = structured.invoke.call_args.args[0]
        user_content = messages[1].content
        assert "PDF EXTRACTED TEXT 12345" in user_content
        assert "Amazon" in user_content  # from body or subject
