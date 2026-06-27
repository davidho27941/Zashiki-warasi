"""Behavioural tests for EmailAgent.

The LLM is mocked out — we never actually invoke a model. The
checkpointer uses LangGraph's `InMemorySaver` and the DB uses
SQLite in memory, so these tests run in <1s with no external
dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.core.models import Base
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import EmailAnalysis, EmailMessage


@dataclass
class MockChat:
    """Bundle returned by the mock_chat_model fixture.

    `model` is the BaseChatModel mock (assert against
    with_structured_output);  `structured` is the runnable returned by
    .with_structured_output() (assert against invoke).
    """

    model: MagicMock
    structured: MagicMock


# ---------- fixtures ----------


@pytest.fixture
def session_factory():
    """In-memory SQLite with our domain tables created on the fly."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture
def fake_email() -> EmailMessage:
    return EmailMessage(
        id="msg-abc",
        thread_id="thread-1",
        history_id=12345,
        from_address="alice@example.com",
        to_addresses=["me@example.com"],
        cc_addresses=[],
        subject="Quarterly report",
        snippet="Here is the Q2 report.",
        body_plain="Please review the attached quarterly report by Friday.",
        body_html="<p>Please review the attached quarterly report.</p>",
        received_at=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        labels=["INBOX", "UNREAD"],
        attachments=[],
    )


@pytest.fixture
def fixed_analysis() -> EmailAnalysis:
    return EmailAnalysis(
        importance=4,
        urgency="urgent",
        category="會議邀請",
        summary="Quarterly report needs review by Friday.",
        keywords=["report", "Q2", "Friday"],
    )


@pytest.fixture
def mock_chat_model(monkeypatch, fixed_analysis) -> MockChat:
    """Patch get_chat_model so EmailAgent's analyze node uses a mock."""
    structured = MagicMock(name="structured_output")
    structured.invoke.return_value = fixed_analysis

    model = MagicMock(name="chat_model")
    model.with_structured_output.return_value = structured

    monkeypatch.setattr(
        "zashiki_warasi.agents.email_agent.get_chat_model",
        lambda: model,
    )
    return MockChat(model=model, structured=structured)


@pytest.fixture
def mock_notifier() -> MagicMock:
    """Default mock for tests that don't care about telegram delivery."""
    return MagicMock(name="notifier")


@pytest.fixture
def mock_client() -> MagicMock:
    """GmailClient mock — expense subgraph uses it for get_attachment."""
    return MagicMock(name="gmail_client")


@pytest.fixture
def agent(
    session_factory, mock_chat_model, mock_notifier, mock_client
) -> EmailAgent:
    return EmailAgent(
        checkpointer=InMemorySaver(),
        session_factory=session_factory,
        notifier=mock_notifier,
        client=mock_client,
    )


def _count_analyses(session_factory) -> int:
    with session_factory() as session:
        return session.scalar(
            select(func.count()).select_from(EmailAnalysisORM)
        )


# ---------- persistence ----------


class TestPersistence:
    def test_creates_row_with_correct_fields(
        self, agent, fake_email, session_factory
    ):
        agent.handle_email(fake_email)

        with session_factory() as session:
            row = session.get(EmailAnalysisORM, fake_email.id)

        assert row is not None
        assert row.message_id == fake_email.id
        assert row.category == "會議邀請"
        assert row.importance == 4
        assert row.urgency == "urgent"
        assert row.summary == "Quarterly report needs review by Friday."
        assert row.keywords == ["report", "Q2", "Friday"]

    def test_analyzed_at_populated(
        self, agent, fake_email, session_factory
    ):
        agent.handle_email(fake_email)
        with session_factory() as session:
            row = session.get(EmailAnalysisORM, fake_email.id)
        assert row.analyzed_at is not None


# ---------- idempotency ----------


class TestIdempotency:
    def test_second_call_does_not_duplicate(
        self, agent, fake_email, session_factory
    ):
        agent.handle_email(fake_email)
        agent.handle_email(fake_email)
        assert _count_analyses(session_factory) == 1

    def test_different_emails_create_separate_rows(
        self, agent, fake_email, session_factory
    ):
        agent.handle_email(fake_email)
        other = fake_email.model_copy(update={"id": "msg-xyz"})
        agent.handle_email(other)
        assert _count_analyses(session_factory) == 2


# ---------- LLM invocation ----------


class TestLLMInvocation:
    def test_llm_invoked_once_per_email(
        self, agent, fake_email, mock_chat_model
    ):
        agent.handle_email(fake_email)
        assert mock_chat_model.structured.invoke.call_count == 1

    def test_with_structured_output_uses_email_analysis_schema(
        self, agent, mock_chat_model
    ):
        # Analyze uses EmailAnalysis; the expense subgraph (built in
        # __init__) also calls with_structured_output(ExpenseDraft) on
        # the same model. Assert EmailAnalysis is among the calls.
        from zashiki_warasi.core.schemas import ExpenseDraft

        targets = [
            c.args[0]
            for c in mock_chat_model.model.with_structured_output.call_args_list
        ]
        assert EmailAnalysis in targets
        assert ExpenseDraft in targets

    def test_system_prompt_starts_correctly(
        self, agent, fake_email, mock_chat_model
    ):
        agent.handle_email(fake_email)
        messages = mock_chat_model.structured.invoke.call_args.args[0]
        # New prompt is Chinese; check for a stable marker phrase
        assert "電子郵件分析助理" in messages[0].content

    def test_user_prompt_contains_email_fields(
        self, agent, fake_email, mock_chat_model
    ):
        agent.handle_email(fake_email)
        messages = mock_chat_model.structured.invoke.call_args.args[0]
        user_content = messages[1].content
        assert "alice@example.com" in user_content
        assert "Quarterly report" in user_content
        assert "2026-06-22" in user_content
        assert "Please review the attached" in user_content

    def test_body_plain_preferred_over_snippet(
        self, agent, fake_email, mock_chat_model
    ):
        agent.handle_email(fake_email)
        user_content = mock_chat_model.structured.invoke.call_args.args[0][1].content
        assert "Please review the attached" in user_content
        # snippet text is a different string in our fixture
        assert "Here is the Q2 report." not in user_content

    def test_falls_back_to_snippet_when_body_plain_missing(
        self, agent, mock_chat_model
    ):
        email = EmailMessage(
            id="msg-no-body",
            thread_id="t-x",
            history_id=200,
            from_address="x@y.com",
            subject="No body",
            snippet="snippet only",
            body_plain=None,
            received_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        agent.handle_email(email)
        user_content = mock_chat_model.structured.invoke.call_args.args[0][1].content
        assert "snippet only" in user_content


# ---------- None / graceful handling ----------


class TestNoneAnalysis:
    def test_no_persist_when_analysis_is_none(
        self, monkeypatch, session_factory, fake_email
    ):
        structured = MagicMock()
        structured.invoke.return_value = None
        model = MagicMock()
        model.with_structured_output.return_value = structured
        monkeypatch.setattr(
            "zashiki_warasi.agents.email_agent.get_chat_model",
            lambda: model,
        )

        agent = EmailAgent(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            notifier=MagicMock(),
            client=MagicMock(),
        )

        # Must not raise
        agent.handle_email(fake_email)
        assert _count_analyses(session_factory) == 0


# ---------- telegram notify node ----------


class TestNotifyNode:
    def test_notifier_called_once_per_email(
        self, agent, fake_email, mock_notifier
    ):
        agent.handle_email(fake_email)
        assert mock_notifier.send_message.call_count == 1

    def test_message_contains_category_and_importance(
        self, agent, fake_email, mock_notifier
    ):
        agent.handle_email(fake_email)
        text = mock_notifier.send_message.call_args.args[0]
        # New format: [category] in header, ★ for importance
        assert "[會議邀請]" in text
        assert "★" * 4 + "☆" in text  # importance=4 → 4 stars + 1 empty
        # urgency Chinese label
        assert "緊急" in text

    def test_message_contains_from_subject_summary(
        self, agent, fake_email, mock_notifier
    ):
        agent.handle_email(fake_email)
        text = mock_notifier.send_message.call_args.args[0]
        assert "alice@example.com" in text
        assert "Quarterly report" in text
        assert "needs review by Friday" in text

    def test_message_escapes_html_in_user_fields(
        self, agent, mock_chat_model, mock_notifier
    ):
        from zashiki_warasi.core.schemas import EmailAnalysis

        # Inject analysis whose summary has HTML special chars.
        mock_chat_model.structured.invoke.return_value = EmailAnalysis(
            importance=2,
            urgency="none",
            category="其他",
            summary="Review <code> blocks & semicolons",
            keywords=[],
        )
        email = EmailMessage(
            id="msg-html",
            thread_id="t",
            history_id=1,
            from_address="<script>@x.com",
            subject="<b>injected</b>",
            received_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        agent.handle_email(email)

        text = mock_notifier.send_message.call_args.args[0]
        assert "<script>@x.com" not in text
        assert "&lt;script&gt;@x.com" in text
        assert "<b>injected</b>" not in text
        assert "&lt;b&gt;injected&lt;/b&gt;" in text
        assert "&lt;code&gt; blocks &amp; semicolons" in text

    def test_notifier_failure_blocks_persistence(
        self, agent, fake_email, session_factory, mock_notifier
    ):
        mock_notifier.send_message.side_effect = RuntimeError(
            "telegram unreachable"
        )

        with pytest.raises(RuntimeError, match="telegram unreachable"):
            agent.handle_email(fake_email)

        # No analysis row written — handler exception aborts handle_email
        # before _persist runs. Next tick will retry and (because the
        # checkpoint cached analyze) skip LLM but re-run notify.
        assert _count_analyses(session_factory) == 0

    def test_second_call_after_notify_success_does_not_resend(
        self, agent, fake_email, mock_notifier
    ):
        agent.handle_email(fake_email)
        agent.handle_email(fake_email)
        # Idempotency: LangGraph checkpoint short-circuits both analyze
        # and notify on the second call.
        assert mock_notifier.send_message.call_count == 1

    def test_notify_runs_after_analyze_in_graph(
        self, agent, fake_email, mock_chat_model, mock_notifier
    ):
        # Order assertion via MagicMock parent: attach both as children
        # of a shared parent and inspect mock_calls.
        parent = MagicMock()
        parent.attach_mock(mock_chat_model.structured.invoke, "analyze")
        parent.attach_mock(mock_notifier.send_message, "notify")

        agent.handle_email(fake_email)

        method_order = [c[0] for c in parent.mock_calls]
        assert method_order.index("analyze") < method_order.index("notify")


# ---------- routing: analyze -> expense_sg vs notify ----------


class TestRouting:
    def test_non_expense_category_skips_expense_subgraph(
        self, agent, fake_email, mock_notifier, mock_client
    ):
        # fixed_analysis uses category="會議邀請" (non-expense).
        agent.handle_email(fake_email)
        # Expense subgraph would call client.get_attachment for any PDF
        # attachments; with no PDFs and a non-expense category, the
        # subgraph never runs and get_attachment is never called.
        mock_client.get_attachment.assert_not_called()
        # notify still happens
        mock_notifier.send_message.assert_called_once()

    def test_expense_category_invokes_expense_subgraph(
        self, monkeypatch, session_factory, fake_email,
        mock_notifier, mock_client,
    ):
        from decimal import Decimal
        from zashiki_warasi.core.schemas import (
            EmailAnalysis,
            ExpenseDraft,
            ExpenseLogged,
        )

        # Analyze returns category="消費支出" → routes to expense_sg.
        expense_analysis = EmailAnalysis(
            importance=3,
            urgency="normal",
            category="消費支出",
            summary="Amazon 訂單確認",
            keywords=["Amazon"],
        )
        draft = ExpenseDraft(
            amount=Decimal("3200"),
            currency="JPY",
            vendor="Amazon.co.jp",
        )

        # mock_chat_model fixture builds ONE structured runnable;
        # analyze and the subgraph each call with_structured_output()
        # → different runnable per call. We need a model where each
        # call returns a distinct mock.
        analyze_runnable = MagicMock(name="analyze_structured")
        analyze_runnable.invoke.return_value = expense_analysis
        extract_runnable = MagicMock(name="extract_structured")
        extract_runnable.invoke.return_value = draft

        model = MagicMock(name="chat_model")
        # First call wraps EmailAnalysis (in EmailAgent.__init__),
        # second wraps ExpenseDraft (in ExpenseSubgraph).
        model.with_structured_output.side_effect = [
            analyze_runnable,
            extract_runnable,
        ]
        monkeypatch.setattr(
            "zashiki_warasi.agents.email_agent.get_chat_model",
            lambda: model,
        )

        agent = EmailAgent(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            notifier=mock_notifier,
            client=mock_client,
        )

        agent.handle_email(fake_email)

        # Both LLM runnables invoked exactly once.
        analyze_runnable.invoke.assert_called_once()
        extract_runnable.invoke.assert_called_once()

        # Notify message contains the ExpenseLogged block markers.
        text = mock_notifier.send_message.call_args.args[0]
        assert "已記帳" in text
        assert "3200 JPY" in text
        assert "Amazon.co.jp" in text


# ---------- needs_review side_effect rendering ----------


class TestNeedsReviewNotify:
    def test_image_pdf_unreadable_message_contains_warning_and_filename(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_needs_review,
        )
        from zashiki_warasi.core.schemas import ExpenseNeedsReview

        effect = ExpenseNeedsReview(
            reason="image_pdf_unreadable",
            unreadable_attachments=["scan_receipt.pdf"],
        )
        text = _format_expense_needs_review(effect)
        assert "⚠️" in text
        assert "影像格式" in text
        assert "scan_receipt.pdf" in text
        assert "人工" in text

    def test_extraction_yielded_nulls_message_explains_reason(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_needs_review,
        )
        from zashiki_warasi.core.schemas import ExpenseNeedsReview

        effect = ExpenseNeedsReview(reason="extraction_yielded_nulls")
        text = _format_expense_needs_review(effect)
        assert "信件內容不足" in text


# ---------- expense_logged rendering ----------


class TestExpenseLoggedNotify:
    def test_all_fields_present(self):
        from decimal import Decimal
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            amount=Decimal("3200"),
            currency="JPY",
            vendor="Amazon.co.jp",
            location="東京都渋谷区",
            category="購物",
            transacted_at=datetime(2026, 6, 27, 14, 32, tzinfo=timezone.utc),
            payment_method="SMBC Olive",
            transaction_id="250-1234567",
        )
        text = _format_expense_logged(effect)
        assert "已記帳" in text
        assert "3200 JPY" in text
        assert "Amazon.co.jp" in text
        assert "東京都渋谷区" in text
        assert "購物" in text
        assert "2026-06-27 14:32" in text
        assert "SMBC Olive" in text
        assert "250-1234567" in text
        # Real id (no AUTO- prefix) doesn't get the "(自動編號)" suffix
        assert "(自動編號)" not in text

    def test_auto_transaction_id_marked_in_message(self):
        from decimal import Decimal
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            amount=Decimal("100"),
            currency="JPY",
            vendor="V",
            location=None,
            category=None,
            transacted_at=None,
            payment_method=None,
            transaction_id="AUTO-deadbeef1234",
        )
        text = _format_expense_logged(effect)
        assert "AUTO-deadbeef1234" in text
        assert "(自動編號)" in text

    def test_missing_amount_displays_buming(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            amount=None,
            currency=None,
            vendor="某店",
            location=None,
            category=None,
            transacted_at=None,
            payment_method=None,
            transaction_id=None,
        )
        text = _format_expense_logged(effect)
        # Every nullable field should fall back to 不明
        assert text.count("不明") >= 6  # amount, location, category,
                                        # time, payment, transaction_id

    def test_other_payment_method_shown_with_warning(self):
        from decimal import Decimal
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            amount=Decimal("100"),
            currency="JPY",
            vendor="V",
            location=None,
            category=None,
            transacted_at=None,
            payment_method="其他",
            transaction_id=None,
        )
        text = _format_expense_logged(effect)
        assert "⚠️ 其他" in text
        assert "請檢查信件確認" in text
