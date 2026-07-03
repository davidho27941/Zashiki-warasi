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
from zashiki_warasi.core.schemas import (
    AnalysisFailed,
    EmailAnalysis,
    EmailMessage,
)


def _make_length_finish_error(
    prompt_tokens: int = 31059, completion_tokens: int = 1709
):
    """Build a real `LengthFinishReasonError` — its `__init__` needs a
    valid ChatCompletion, so we synthesise a minimal one instead of
    reaching for `MagicMock(spec=...)` which would fail pydantic
    validation in `_parse_chat_completion`."""
    from openai import LengthFinishReasonError
    from openai.types.chat.chat_completion import (
        ChatCompletion,
        ChatCompletionMessage,
        Choice,
    )
    from openai.types.completion_usage import CompletionUsage

    completion = ChatCompletion(
        id="c-1",
        created=0,
        model="test",
        object="chat.completion",
        choices=[
            Choice(
                index=0,
                message=ChatCompletionMessage(
                    role="assistant", content="partial"
                ),
                finish_reason="length",
            )
        ],
        usage=CompletionUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
        ),
    )
    return LengthFinishReasonError(completion=completion)


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

    def test_system_prompt_lists_aggregate_categories(
        self, agent, fake_email, mock_chat_model
    ):
        # 點數資訊彙整 and 消費資訊彙整 were added so the LLM doesn't
        # have to fall back to 其他 for Rakuten point / credit-card
        # roll-up notifications.
        agent.handle_email(fake_email)
        prompt = mock_chat_model.structured.invoke.call_args.args[0][0].content
        assert "點數資訊彙整" in prompt
        assert "消費資訊彙整" in prompt

    def test_system_prompt_disambiguates_points_vs_expense(
        self, agent, fake_email, mock_chat_model
    ):
        # Rakuten / d points must NOT route to the expense subgraph.
        agent.handle_email(fake_email)
        prompt = mock_chat_model.structured.invoke.call_args.args[0][0].content
        assert "點數消費" in prompt or "點數" in prompt
        # The rule must explicitly say it's not a 消費支出.
        assert "不視為「消費支出」" in prompt or "不要" in prompt

    def test_system_prompt_requires_payment_specifics_in_summary(
        self, agent, fake_email, mock_chat_model
    ):
        # We deliberately reverted the earlier "do NOT include
        # amounts/time/vendor in summary" rule — too much info was
        # lost for non-expense emails that have no structured-data
        # fallback (points, aggregates, bills).
        agent.handle_email(fake_email)
        prompt = mock_chat_model.structured.invoke.call_args.args[0][0].content
        assert "金額" in prompt
        assert "時間" in prompt
        # Either 地點 (location) or 來源 (source) should appear.
        assert "地點" in prompt or "來源" in prompt

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

    def test_falls_back_to_html_when_body_plain_missing(
        self, agent, mock_chat_model
    ):
        # HTML-only email (modern e-receipt). Analyze sees the
        # converted HTML rather than the ~200-char snippet.
        email = EmailMessage(
            id="msg-html-only",
            thread_id="t-h",
            history_id=300,
            from_address="auto@merchant.example",
            subject="Receipt",
            snippet="short preview",
            body_plain=None,
            body_html=(
                "<html><body>"
                "<h2>Order Confirmation</h2>"
                "<p>Total: <b>¥1,198</b></p>"
                "<p>Vendor: スターバックス 渋谷店</p>"
                "</body></html>"
            ),
            received_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        agent.handle_email(email)
        user_content = mock_chat_model.structured.invoke.call_args.args[0][1].content
        # HTML text was converted and reached the prompt.
        assert "¥1,198" in user_content
        assert "スターバックス 渋谷店" in user_content
        assert "Order Confirmation" in user_content
        # Snippet was NOT used as fallback (HTML came first).
        assert "short preview" not in user_content

    def test_plain_preferred_over_html_when_both_present(
        self, agent, mock_chat_model
    ):
        # multipart/alternative — plain wins to keep prompt clean.
        email = EmailMessage(
            id="msg-both",
            thread_id="t-b",
            history_id=400,
            from_address="x@y.com",
            subject="Both",
            snippet="snippet",
            body_plain="PLAIN BODY",
            body_html="<p>HTML BODY VERSION</p>",
            received_at=datetime(2026, 6, 22, tzinfo=timezone.utc),
        )
        agent.handle_email(email)
        user_content = mock_chat_model.structured.invoke.call_args.args[0][1].content
        assert "PLAIN BODY" in user_content
        assert "HTML BODY VERSION" not in user_content


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

    @pytest.mark.parametrize(
        "category", ["消費支出", "帳單通知"]
    )
    def test_expense_like_categories_route_to_expense(self, category):
        # Direct unit check on the router. Avoids the full subgraph
        # scaffolding — used by the integration test below — since the
        # dispatch logic is the whole point here.
        agent = EmailAgent.__new__(EmailAgent)
        state = {
            "analysis": EmailAnalysis(
                importance=3,
                urgency="normal",
                category=category,
                summary="s",
                keywords=[],
            )
        }
        assert agent._route_by_category(state) == "expense"

    @pytest.mark.parametrize(
        "category",
        [
            "廣告",
            "促銷",
            "點數資訊彙整",
            "消費資訊彙整",
            "訂閱服務",
            "會議邀請",
            "其他",
        ],
    )
    def test_non_expense_categories_route_to_notify(self, category):
        # Regression guard: adding 帳單通知 must NOT also grab 消費資訊
        # 彙整 (multi-txn digests, no per-line detail) or 點數資訊彙整
        # (point notifications, deliberately excluded from expense).
        agent = EmailAgent.__new__(EmailAgent)
        state = {
            "analysis": EmailAnalysis(
                importance=3,
                urgency="normal",
                category=category,
                summary="s",
                keywords=[],
            )
        }
        assert agent._route_by_category(state) == "notify"

    def test_missing_analysis_routes_to_notify(self):
        agent = EmailAgent.__new__(EmailAgent)
        assert agent._route_by_category({"analysis": None}) == "notify"

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


# ---------- prompt content regression guards ----------


class TestAnalyzePromptRules:
    """These tests pin specific rules into the analyze prompt so a
    future rewrite cannot silently drop them. Each rule was added in
    response to a concrete misclassification observed in production;
    the comment above each assertion is the incident it guards."""

    def test_boilerplate_disclaimer_rule_present(self):
        # An SMBC Olive デビット notification (single transaction at
        # SEVEN-ELEVEN for 280 JPY) had a boilerplate line about
        # overseas ATM fees (110 JPY). The LLM listed the 110 as a
        # second transaction in the summary. Rule must remind the
        # model that boilerplate disclaimers ≠ transactions.
        from zashiki_warasi.agents.email_agent import ANALYZE_SYSTEM_PROMPT

        assert "反幻想" in ANALYZE_SYSTEM_PROMPT
        assert "boilerplate" in ANALYZE_SYSTEM_PROMPT
        assert "手續費說明" in ANALYZE_SYSTEM_PROMPT

    def test_single_txn_with_boilerplate_stays_expense_category(self):
        # Same SMBC Olive incident: the mail was misclassified as
        # `消費資訊彙整` (multi-transaction digest) because the
        # boilerplate line about ATM fees was interpreted as a second
        # transaction. Rule must clarify that single-transaction +
        # boilerplate stays `消費支出`.
        from zashiki_warasi.agents.email_agent import ANALYZE_SYSTEM_PROMPT

        assert "SMBC Olive" in ANALYZE_SYSTEM_PROMPT
        assert "承認番号" in ANALYZE_SYSTEM_PROMPT
        # Cross-reference the specific number so a rewrite that keeps
        # "SMBC Olive" but loses the concrete example still trips.
        assert "110" in ANALYZE_SYSTEM_PROMPT


class TestExpenseExtractPromptRules:
    def test_boilerplate_vs_transaction_rule_present(self):
        # If the classification is fixed but a similar mail slips
        # through, we still don't want the extractor to fill
        # `amount=110` from the ATM-fee disclaimer. Rule must guide
        # the LLM to distinguish transaction fields from disclaimer
        # text.
        from zashiki_warasi.agents.verticals.expense import (
            EXPENSE_EXTRACT_SYSTEM_PROMPT,
        )

        assert "條款" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        assert "boilerplate" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        assert "SMBC Olive" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        assert "承認番号" in EXPENSE_EXTRACT_SYSTEM_PROMPT

    def test_positive_amount_extraction_markers_present(self):
        # A first pass at rule 10 was too defensive ("boilerplate
        # 通常只有金額 → 這種數字不是這筆消費的金額") — the local
        # LLM over-applied it and returned amount=None for a valid
        # 803 JPY SMBC Olive charge. The fix reframes rule 10 as
        # positive pattern-matching: show the model the four ◇
        # field mapping and require it to extract each one. These
        # pins guard the positive markers.
        from zashiki_warasi.agents.verticals.expense import (
            EXPENSE_EXTRACT_SYSTEM_PROMPT,
        )

        assert "◇利用金額" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        assert "◇利用日" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        assert "◇利用先" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        # The '必須' word specifically guards against the over-cautious
        # "回 null" default the previous phrasing invited.
        assert "必須" in EXPENSE_EXTRACT_SYSTEM_PROMPT
        # Concrete 803 example so a rewrite that keeps only the
        # abstract mapping still trips.
        assert "803" in EXPENSE_EXTRACT_SYSTEM_PROMPT


# ---------- LLM analyze failure handling ----------


class TestAnalyzeFailure:
    """`_analyze` must not let an `openai.LengthFinishReasonError`
    escape the graph — it'd surface as an unhandled poller-tick
    error and re-fire on every retry until Gmail history retention
    kicks in. Instead the node emits an `AnalysisFailed` side_effect
    so notify still pings the user for manual review."""

    @pytest.fixture
    def agent_with_length_error(
        self, monkeypatch, session_factory, mock_notifier, mock_client
    ):
        # Structured runnable raises LengthFinishReasonError on invoke.
        structured = MagicMock(name="structured_output")
        structured.invoke.side_effect = _make_length_finish_error()
        model = MagicMock(name="chat_model")
        model.with_structured_output.return_value = structured
        monkeypatch.setattr(
            "zashiki_warasi.agents.email_agent.get_chat_model",
            lambda: model,
        )
        return EmailAgent(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            notifier=mock_notifier,
            client=mock_client,
        )

    def test_length_error_does_not_escape_handle_email(
        self, agent_with_length_error, fake_email
    ):
        # No pytest.raises — the graph must return normally.
        agent_with_length_error.handle_email(fake_email)

    def test_length_error_sends_notify_message(
        self, agent_with_length_error, fake_email, mock_notifier
    ):
        agent_with_length_error.handle_email(fake_email)

        mock_notifier.send_message.assert_called_once()
        text = mock_notifier.send_message.call_args.args[0]
        assert "LLM 分析失敗" in text
        assert "token 上限" in text
        # Detail (prompt/completion token counts) is surfaced so the
        # user can eyeball whether it's a genuinely huge email or a
        # runaway generation.
        assert "31059" in text
        assert "1709" in text
        # Subject and sender still rendered so the user can locate the
        # offending mail without cross-referencing an id.
        assert fake_email.subject in text
        assert fake_email.from_address in text

    def test_length_error_does_not_persist_analysis(
        self, agent_with_length_error, fake_email, session_factory
    ):
        # Persistence guard: without an EmailAnalysis object we must
        # NOT write a placeholder row — analyzer failures shouldn't
        # pollute the analytics table.
        agent_with_length_error.handle_email(fake_email)
        assert _count_analyses(session_factory) == 0

    def test_length_error_only_calls_llm_once(
        self, agent_with_length_error, fake_email
    ):
        # Regression guard: the graph must not retry the analyze node
        # after catching the error (a retry would just re-raise).
        agent_with_length_error.handle_email(fake_email)
        structured = (
            agent_with_length_error._analyze_model
        )
        structured.invoke.assert_called_once()


class TestFormatAnalysisFailed:
    """Direct tests on the formatter — subject / sender injection
    guard and reason text coverage."""

    def _email(self, **overrides) -> EmailMessage:
        base = dict(
            id="m1",
            thread_id="t",
            history_id=1,
            from_address="billing@gcp.example",
            subject="Your invoice is attached",
            received_at=datetime(2026, 7, 2, tzinfo=timezone.utc),
        )
        base.update(overrides)
        return EmailMessage(**base)

    def test_content_too_long_reason_renders(self):
        from zashiki_warasi.agents.email_agent import (
            _format_analysis_failed,
        )

        effect = AnalysisFailed(
            reason="content_too_long",
            detail="prompt=31059 completion=1709",
        )
        text = _format_analysis_failed(self._email(), effect)
        assert "⚠️" in text
        assert "LLM 分析失敗" in text
        assert "token 上限" in text
        assert "prompt=31059" in text
        assert "手動" in text

    def test_html_special_chars_in_subject_are_escaped(self):
        from zashiki_warasi.agents.email_agent import (
            _format_analysis_failed,
        )

        effect = AnalysisFailed(reason="content_too_long")
        email = self._email(
            subject="<script>x</script>",
            from_address="<b>a</b>@x.com",
        )
        text = _format_analysis_failed(email, effect)
        assert "<script>x</script>" not in text
        assert "&lt;script&gt;" in text
        assert "<b>a</b>@x.com" not in text

    def test_detail_omitted_when_none(self):
        from zashiki_warasi.agents.email_agent import (
            _format_analysis_failed,
        )

        effect = AnalysisFailed(reason="content_too_long", detail=None)
        text = _format_analysis_failed(self._email(), effect)
        assert "用量:" not in text


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
            title="Kindle Paperwhite",
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
        assert "Kindle Paperwhite" in text  # title in headline
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
            title=None,
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

    def test_title_renders_in_headline(self):
        from decimal import Decimal
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            title="拿鐵 + 摩卡星冰樂",
            amount=Decimal("1198"),
            currency="JPY",
            vendor="Starbucks",
            location=None,
            category="飲食",
            transacted_at=None,
            payment_method="現金",
            transaction_id=None,
        )
        text = _format_expense_logged(effect)
        # Title is appended to the "💰 已記帳" headline (HTML tag
        # closes before the colon).
        assert "💰 <b>已記帳</b>: 拿鐵 + 摩卡星冰樂" in text

    def test_no_title_keeps_plain_headline(self):
        from decimal import Decimal
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            title=None,  # LLM didn't extract one
            amount=Decimal("100"),
            currency="JPY",
            vendor="V",
            location=None,
            category=None,
            transacted_at=None,
            payment_method=None,
            transaction_id=None,
        )
        text = _format_expense_logged(effect)
        assert "已記帳:" not in text  # no trailing colon when no title
        assert "💰 <b>已記帳</b>" in text


class TestNotionLinkInNotify:
    """`_format_expense_logged` renders Notion status from the
    SideEffect: a clickable link on success, a warning line on failure,
    nothing when Notion is not configured."""

    def _logged(self, **overrides):
        from decimal import Decimal
        from zashiki_warasi.core.schemas import ExpenseLogged

        base = dict(
            record_id="uuid-x",
            title=None,
            amount=Decimal("100"),
            currency="JPY",
            vendor="V",
            location=None,
            category=None,
            transacted_at=None,
            payment_method=None,
            transaction_id="AUTO-deadbeef1234",
            notion_page_id=None,
            notion_sync_error=None,
        )
        base.update(overrides)
        return ExpenseLogged(**base)

    def test_notion_page_id_renders_as_link(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )

        # Notion page ids in API responses come with dashes; the public
        # URL is the same id with dashes stripped.
        effect = self._logged(
            notion_page_id="1234abcd-5678-ef90-1234-567890abcdef"
        )
        text = _format_expense_logged(effect)
        assert "https://notion.so/1234abcd5678ef901234567890abcdef" in text
        assert "🔗" in text

    def test_notion_sync_error_renders_as_warning(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )

        effect = self._logged(
            notion_sync_error="connection reset by peer"
        )
        text = _format_expense_logged(effect)
        assert "⚠️ Notion 同步失敗" in text
        assert "connection reset by peer" in text

    def test_long_error_message_truncated(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )

        long_err = "x" * 500
        effect = self._logged(notion_sync_error=long_err)
        text = _format_expense_logged(effect)
        # Truncated to 80 chars in the formatter to keep Telegram tidy.
        assert "x" * 80 in text
        assert "x" * 200 not in text

    def test_neither_field_renders_nothing(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )

        text = _format_expense_logged(self._logged())
        assert "https://notion.so" not in text
        assert "Notion 同步失敗" not in text

    def test_page_id_takes_precedence_over_error(self):
        # Defensive: in normal operation only one is set, but if both
        # somehow appear we prefer the success link.
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )

        effect = self._logged(
            notion_page_id="abcd1234",
            notion_sync_error="should be ignored",
        )
        text = _format_expense_logged(effect)
        assert "https://notion.so/abcd1234" in text
        assert "Notion 同步失敗" not in text

    def test_missing_amount_displays_buming(self):
        from zashiki_warasi.agents.email_agent import (
            _format_expense_logged,
        )
        from zashiki_warasi.core.schemas import ExpenseLogged

        effect = ExpenseLogged(
            record_id="uuid-x",
            title=None,
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
            title=None,
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
