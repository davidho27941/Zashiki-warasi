"""LayaShadowClient behavior (v1.6.0 §6.6).

Non-interference is the load-bearing contract: every failure mode must
end inside the client (metric + optional row), never in the caller.
"""

from __future__ import annotations

import json
import pathlib
from datetime import datetime, timezone

import httpx
import pytest
from prometheus_client import generate_latest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from zashiki_warasi.classifier.laya_shadow_client import LayaShadowClient
from zashiki_warasi.core.config import LayaSettings
from zashiki_warasi.core.models import Base, LayaShadowPrediction
from zashiki_warasi.core.schemas import EmailMessage
from zashiki_warasi.observability import REGISTRY

REPO = pathlib.Path(__file__).resolve().parents[2]
DICT_PATH = str(REPO / "deploy/helm/zashiki-warasi/configs/laya-questions.json")


def _email(msg_id="m-shadow-1") -> EmailMessage:
    return EmailMessage(
        id=msg_id, thread_id="t", history_id=1,
        from_address="x@example.com", subject="測試",
        body_plain="內文",
        received_at=datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc),
    )


def _settings(**over) -> LayaSettings:
    base = dict(
        shadow_enabled=True,
        base_url="http://laya.test:8080",
        questions_path=DICT_PATH,
        semantic_ver="test-v1",
        timeout_seconds=2.0,
    )
    base.update(over)
    return LayaSettings(**base)


@pytest.fixture()
def session_factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)


def _counter(family: str, selector: str) -> float:
    total = 0.0
    for line in generate_latest(REGISTRY).decode().splitlines():
        if line.startswith(family) and selector in line:
            total += float(line.rsplit(" ", 1)[1])
    return total


def _jev_response(choice: str, confidence: float = 0.9,
                  probs: dict | None = None) -> dict:
    probs = probs or {choice: confidence}
    return {
        "model": "laya-rl-agent",
        "answers": {"category": {
            "type": "choice", "choice": choice,
            "probabilities": probs, "confidence": confidence,
            "answer_confidence": confidence,
        }},
        "usage": {"input_tokens": 10, "output_tokens": 0},
    }


class TestKillSwitch:
    def test_disabled_by_default_is_inert(self, session_factory):
        client = LayaShadowClient(
            settings=LayaSettings(shadow_enabled=False, base_url=""),
            session_factory=session_factory,
        )
        assert client.enabled is False
        assert client.classify_async(
            email=_email(), llm_category="廣告") is None
        with session_factory() as s:
            assert s.scalar(select(LayaShadowPrediction)) is None

    def test_flag_without_url_stays_inert(self, session_factory):
        client = LayaShadowClient(
            settings=LayaSettings(shadow_enabled=True, base_url=""),
            session_factory=session_factory,
        )
        assert client.enabled is False


class TestSuccessPath:
    def test_row_and_agreement_metric(
        self, session_factory, monkeypatch
    ):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json=_jev_response("marketing", 0.91,
                                    {"marketing": 0.91, "promotion": 0.06}),
            request=httpx.Request("POST", "http://laya.test"),
        ))
        before = _counter(
            "zashiki_classifier_shadow_agreement_total",
            'agreed="true",laya_category="廣告",llm_category="廣告"')
        fut = client.classify_async(email=_email("m-ok"), llm_category="廣告")
        fut.result(timeout=5)
        after = _counter(
            "zashiki_classifier_shadow_agreement_total",
            'agreed="true",laya_category="廣告",llm_category="廣告"')
        assert after == before + 1.0
        with session_factory() as s:
            row = s.scalar(select(LayaShadowPrediction).where(
                LayaShadowPrediction.message_id == "m-ok"))
            assert row.laya_category == "廣告"
            assert row.llm_category == "廣告"
            assert row.laya_error is None
            assert row.input_text.startswith("測試")
            assert row.laya_model_ver.startswith("test-v1-")
            assert row.laya_alternates[0]["cat"] == "廣告"

    def test_calendar_superclass_agreement(
        self, session_factory, monkeypatch
    ):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json=_jev_response("calendar_event", 0.88),
            request=httpx.Request("POST", "http://laya.test"),
        ))
        before = _counter(
            "zashiki_classifier_shadow_agreement_total",
            'agreed="true",laya_category="行事曆事件",llm_category="講座資訊"')
        client.classify_async(
            email=_email("m-cal"), llm_category="講座資訊").result(timeout=5)
        after = _counter(
            "zashiki_classifier_shadow_agreement_total",
            'agreed="true",laya_category="行事曆事件",llm_category="講座資訊"')
        assert after == before + 1.0


class TestFailurePaths:
    @pytest.mark.parametrize("exc,reason", [
        (httpx.ConnectTimeout("t"), "timeout"),
        (httpx.ConnectError("refused"), "connection_refused"),
        (ValueError("garbage"), "bad_response"),
    ])
    def test_errors_record_row_and_metric_without_raising(
        self, session_factory, monkeypatch, exc, reason
    ):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)

        def boom(*a, **k):
            raise exc
        monkeypatch.setattr(httpx, "post", boom)
        before = _counter("zashiki_classifier_shadow_error_total",
                          f'reason="{reason}"')
        msg_id = f"m-{reason}"
        client.classify_async(
            email=_email(msg_id), llm_category="廣告").result(timeout=5)
        after = _counter("zashiki_classifier_shadow_error_total",
                         f'reason="{reason}"')
        assert after == before + 1.0
        with session_factory() as s:
            row = s.scalar(select(LayaShadowPrediction).where(
                LayaShadowPrediction.message_id == msg_id))
            assert row.laya_error == reason
            assert row.laya_category is None
            assert row.input_text  # sample stays usable for training

    def test_http_5xx(self, session_factory, monkeypatch):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            500, text="boom",
            request=httpx.Request("POST", "http://laya.test")))
        client.classify_async(
            email=_email("m-5xx"), llm_category="廣告").result(timeout=5)
        with session_factory() as s:
            row = s.scalar(select(LayaShadowPrediction).where(
                LayaShadowPrediction.message_id == "m-5xx"))
            assert row.laya_error == "http_5xx"

    def test_unmapped_label_is_bad_response(
        self, session_factory, monkeypatch
    ):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json=_jev_response("ghost_label", 0.9),
            request=httpx.Request("POST", "http://laya.test")))
        client.classify_async(
            email=_email("m-ghost"), llm_category="廣告").result(timeout=5)
        with session_factory() as s:
            row = s.scalar(select(LayaShadowPrediction).where(
                LayaShadowPrediction.message_id == "m-ghost"))
            assert row.laya_error == "bad_response"


class TestSaturationDrop:
    def test_drops_when_both_slots_busy(self, session_factory):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        # Occupy both slots manually — a submission now must DROP.
        assert client._slots.acquire(blocking=False)
        assert client._slots.acquire(blocking=False)
        before = _counter("zashiki_classifier_shadow_error_total",
                          'reason="dropped_saturated"')
        assert client.classify_async(
            email=_email("m-drop"), llm_category="廣告") is None
        after = _counter("zashiki_classifier_shadow_error_total",
                         'reason="dropped_saturated"')
        assert after == before + 1.0
        client._slots.release()
        client._slots.release()


class TestNonInterference:
    def test_submit_explosion_is_swallowed(self, session_factory):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)

        class BoomExecutor:
            def submit(self, *a, **k):
                raise RuntimeError("executor detonated")
        client._executor = BoomExecutor()
        # Must return None, must not raise, slot must be released.
        assert client.classify_async(
            email=_email(), llm_category="廣告") is None
        assert client._slots.acquire(blocking=False)  # slot not leaked
        client._slots.release()

    def test_duplicate_insert_is_ignored(
        self, session_factory, monkeypatch
    ):
        client = LayaShadowClient(
            settings=_settings(), session_factory=session_factory)
        monkeypatch.setattr(httpx, "post", lambda *a, **k: httpx.Response(
            200, json=_jev_response("marketing", 0.9),
            request=httpx.Request("POST", "http://laya.test")))
        client.classify_async(
            email=_email("m-dup"), llm_category="廣告").result(timeout=5)
        client.classify_async(
            email=_email("m-dup"), llm_category="廣告").result(timeout=5)
        with session_factory() as s:
            rows = s.scalars(select(LayaShadowPrediction).where(
                LayaShadowPrediction.message_id == "m-dup")).all()
            assert len(rows) == 1
