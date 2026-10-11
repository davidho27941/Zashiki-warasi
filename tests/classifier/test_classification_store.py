"""SCD-2 classification writer (adopt-email-taxonomy-v2, tasks 3.x)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.agents.email_agent import EmailAgent
from zashiki_warasi.classifier import classification_store
from zashiki_warasi.core.models import Base, EmailClassification
from zashiki_warasi.core.models import EmailAnalysis as EmailAnalysisORM
from zashiki_warasi.core.schemas import EmailAnalysis, EmailMessage

T0 = datetime(2026, 10, 1, tzinfo=timezone.utc)


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture()
def session(session_factory):
    with session_factory() as s:
        s.add(EmailAnalysisORM(
            message_id="m1", importance=3, urgency="normal",
            category="消費支出", taxonomy_version="v1", summary="s",
            keywords=[],
        ))
        s.commit()
        yield s


def _rows(session):
    return session.scalars(
        select(EmailClassification)
        .where(EmailClassification.message_id == "m1")
        .order_by(EmailClassification.valid_from)
    ).all()


def _record(session, category="消費支出", version="v1", source="llm",
            at=T0, recorded_by="app"):
    return classification_store.record(
        session, message_id="m1", category=category,
        taxonomy_version=version, source=source,
        recorded_by=recorded_by, at=at,
    )


def _aware(dt):
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class TestRecord:
    def test_first_write_inserts_current_row(self, session):
        assert _record(session) == "inserted"
        (row,) = _rows(session)
        assert row.valid_to is None and _aware(row.valid_from) == T0

    def test_identical_write_is_a_no_op(self, session):
        _record(session)
        assert _record(session, at=T0 + timedelta(hours=1)) == "unchanged"
        assert len(_rows(session)) == 1

    def test_change_closes_old_and_opens_new_at_one_timestamp(self, session):
        _record(session)
        t1 = T0 + timedelta(days=1)
        assert _record(session, category="消費資訊彙整", at=t1) == "versioned"
        old, new = _rows(session)
        assert _aware(old.valid_to) == t1 == _aware(new.valid_from)
        assert new.valid_to is None

    def test_human_overrides_llm(self, session):
        _record(session)
        t1 = T0 + timedelta(days=1)
        assert _record(
            session, category="消費資訊彙整", source="human", at=t1,
            recorded_by="import",
        ) == "versioned"
        assert _rows(session)[-1].source == "human"

    @pytest.mark.parametrize("machine", ["llm", "laya"])
    def test_machine_never_overrides_human(self, session, machine):
        _record(session, source="human", recorded_by="import")
        assert _record(
            session, category="其他", source=machine,
            at=T0 + timedelta(days=1),
        ) == "human_kept"
        (row,) = _rows(session)
        assert row.source == "human" and row.valid_to is None

    def test_human_relabel_under_v2_is_sequential(self, session):
        _record(session)  # v1 LLM row
        t1 = T0 + timedelta(days=1)
        _record(session, category="交易收據", version="v2", source="human",
                at=t1, recorded_by="import")
        old, new = _rows(session)
        assert (old.taxonomy_version, _aware(old.valid_to)) == ("v1", t1)
        assert (new.taxonomy_version, new.valid_to) == ("v2", None)

    def test_category_outside_its_version_rejected(self, session):
        with pytest.raises(ValueError, match="not in taxonomy"):
            _record(session, category="交易收據", version="v1")

    def test_out_of_order_write_rejected(self, session):
        _record(session, at=T0)
        with pytest.raises(ValueError, match="does not follow"):
            _record(session, category="其他", at=T0)

    def test_concurrent_writer_is_re_evaluated(self, session, monkeypatch):
        # Simulate a race: our first read misses a row another writer
        # just committed, so the insert hits the partial unique index.
        _record(session)
        session.commit()
        real = classification_store._current
        calls = {"n": 0}

        def stale_then_real(s, message_id):
            calls["n"] += 1
            return None if calls["n"] == 1 else real(s, message_id)

        monkeypatch.setattr(classification_store, "_current", stale_then_real)
        assert _record(session, at=T0 + timedelta(hours=1)) == "unchanged"
        assert len(_rows(session)) == 1


# ---------- agent integration (LLM path) ----------


@pytest.fixture()
def fake_email():
    return EmailMessage(
        id="msg-1", thread_id="t", history_id=1,
        from_address="a@example.com", to_addresses=["me@example.com"],
        cc_addresses=[], subject="s", snippet="x", body_plain="body",
        body_html=None,
        received_at=datetime(2026, 10, 1, tzinfo=timezone.utc),
        labels=["INBOX"], attachments=[],
    )


@pytest.fixture()
def agent_with(monkeypatch, session_factory):
    def build(analysis):
        structured = MagicMock(name="structured_output")
        structured.invoke.return_value = analysis
        model = MagicMock(name="chat_model")
        model.with_structured_output.return_value = structured
        monkeypatch.setattr(
            "zashiki_warasi.agents.email_agent.get_chat_model",
            lambda *a, **kw: model,
        )
        return EmailAgent(
            checkpointer=InMemorySaver(),
            session_factory=session_factory,
            notifier=MagicMock(name="notifier"),
            client=MagicMock(name="gmail_client"),
        ), structured
    return build


def _history(session_factory, message_id="msg-1"):
    with session_factory() as s:
        return s.scalars(
            select(EmailClassification)
            .where(EmailClassification.message_id == message_id)
        ).all()


class TestAgentPersist:
    def test_first_analysis_writes_analysis_and_history(
        self, agent_with, session_factory, fake_email,
    ):
        agent, _ = agent_with(EmailAnalysis(
            importance=3, urgency="normal", category="社交", summary="s",
        ))
        agent.handle_email(fake_email)
        (row,) = _history(session_factory)
        assert (row.category, row.taxonomy_version, row.source,
                row.recorded_by, row.valid_to) == (
            "社交", "v1", "llm", "app", None)
        with session_factory() as s:
            assert s.get(EmailAnalysisORM, "msg-1").taxonomy_version == "v1"

    def test_replay_does_not_open_a_version(
        self, agent_with, session_factory, fake_email,
    ):
        agent, structured = agent_with(EmailAnalysis(
            importance=3, urgency="normal", category="社交", summary="s",
        ))
        agent.handle_email(fake_email)
        agent.handle_email(fake_email)  # cached analysis reuse path
        assert structured.invoke.call_count == 1
        assert len(_history(session_factory)) == 1

    def test_failed_analysis_writes_no_history(
        self, agent_with, session_factory, fake_email,
    ):
        agent, _ = agent_with(None)
        agent.handle_email(fake_email)
        assert _history(session_factory) == []
