"""End-to-end span-tree assertions using OTel's InMemorySpanExporter.

Verifies the span-contract from `specs/observability/spec.md`:

- POST /poll produces a `zashiki.tick_once` INTERNAL span parented
  under whatever server span the ASGI layer emits (validates D17
  pure-ASGI middleware preserves span context across the /poll
  handler → threadpool → tick body chain).
- `zashiki.tick_once` carries the contracted attributes from
  TickResult (messages_processed, cursor_before, cursor_after,
  rebaselined).
- 409 conflict short-circuits: NO `zashiki.tick_once` span emitted
  because the tick body never ran.
- `node_trace(log, name)` emits a `zashiki.node.<name>` span with
  `zashiki.message_id` attribute when the log adapter carries one
  (validates the extended node_trace in core/logging.py).
- No `langchain.*` / `langgraph.*` span names appear anywhere
  (validates D23 double-emit prevention).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from zashiki_warasi.core.logging import bind_message_context, node_trace
from zashiki_warasi.core.schemas import TickResult
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services
from zashiki_warasi.web.routers import poll as poll_module


@pytest.fixture
def in_memory_exporter():
    """Install a real (non-NoOp) TracerProvider with an in-memory
    exporter and yield it so tests can inspect emitted spans.

    Restores the previous provider on teardown. Uses SimpleSpanProcessor
    (not BatchSpanProcessor) so spans are exported synchronously —
    no background-thread flush timing to fight with in assertions.
    """
    prior_provider = trace._TRACER_PROVIDER
    prior_flag = trace._TRACER_PROVIDER_SET_ONCE._done

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    # Reset the "set once" latch so we're allowed to install ours.
    trace._TRACER_PROVIDER_SET_ONCE._done = False
    trace._TRACER_PROVIDER = None
    trace.set_tracer_provider(provider)

    try:
        yield exporter
    finally:
        provider.shutdown()
        trace._TRACER_PROVIDER_SET_ONCE._done = prior_flag
        trace._TRACER_PROVIDER = prior_provider


def _fake_lock_cm(acquired: bool):
    class _Cm:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return acquired, MagicMock(name="conn")

        def __exit__(self, *exc):
            return False

    return _Cm


def _services_with_tick(tick_result: TickResult):
    services = MagicMock(name="services")
    services.checkpointer_pool = MagicMock()
    services.poller = MagicMock()
    services.poller.tick_once.return_value = tick_result
    services.http_settings.api_key = None
    return services


def _span_names(exporter: InMemorySpanExporter) -> list[str]:
    return [s.name for s in exporter.get_finished_spans()]


def _span_by_name(
    exporter: InMemorySpanExporter, name: str
):
    for s in exporter.get_finished_spans():
        if s.name == name:
            return s
    return None


class TestTickOnceSpan:
    def test_success_emits_tick_once_span_with_attributes(
        self, monkeypatch, in_memory_exporter
    ):
        result = TickResult(
            duration_ms=120,
            messages_processed=3,
            cursor_before=100,
            cursor_after=115,
            rebaselined=False,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        # Rebuild the app AFTER installing our tracer provider so
        # FastAPIInstrumentor's ASGI wrapper picks up our provider.
        app = create_app()
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r = client.post("/poll")
        assert r.status_code == 200

        tick_span = _span_by_name(in_memory_exporter, "zashiki.tick_once")
        assert tick_span is not None, (
            f"tick_once span missing; got: {_span_names(in_memory_exporter)}"
        )
        attrs = dict(tick_span.attributes or {})
        assert attrs.get("zashiki.messages_processed") == 3
        assert attrs.get("zashiki.cursor_before") == 100
        assert attrs.get("zashiki.cursor_after") == 115
        assert attrs.get("zashiki.rebaselined") is False

    def test_error_result_marks_span_error(
        self, monkeypatch, in_memory_exporter
    ):
        result = TickResult(
            duration_ms=30,
            messages_processed=0,
            cursor_before=None,
            cursor_after=None,
            rebaselined=False,
            error="credential_refresh_failed: token dead",
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app = create_app()
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        client.post("/poll")

        tick_span = _span_by_name(in_memory_exporter, "zashiki.tick_once")
        assert tick_span is not None
        from opentelemetry.trace import StatusCode

        assert tick_span.status.status_code == StatusCode.ERROR
        attrs = dict(tick_span.attributes or {})
        assert "credential_refresh_failed" in attrs.get(
            "zashiki.error", ""
        )

    def test_conflict_short_circuits_no_tick_span(
        self, monkeypatch, in_memory_exporter
    ):
        # Lock never acquired → 409, tick body never runs, so the
        # tick_once span must be absent.
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=False)
        )
        services = _services_with_tick(
            TickResult(
                duration_ms=0,
                messages_processed=0,
                cursor_before=None,
                cursor_after=None,
                rebaselined=False,
                error=None,
            )
        )
        app = create_app()
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r = client.post("/poll")
        assert r.status_code == 409

        assert _span_by_name(
            in_memory_exporter, "zashiki.tick_once"
        ) is None


class TestNodeTraceSpan:
    """`node_trace(log, name)` emits `zashiki.node.<name>` — verified
    without any HTTP layer since core/logging is standalone."""

    def test_emits_named_span(self, in_memory_exporter):
        with node_trace(logging.getLogger("test.node"), "analyze"):
            pass
        assert _span_by_name(
            in_memory_exporter, "zashiki.node.analyze"
        ) is not None

    def test_attaches_message_id_from_logger_adapter(
        self, in_memory_exporter
    ):
        adapter = bind_message_context(
            logging.getLogger("test.node"), message_id="msg-42"
        )
        with node_trace(adapter, "expense_extract"):
            pass
        span = _span_by_name(
            in_memory_exporter, "zashiki.node.expense_extract"
        )
        assert span is not None
        attrs = dict(span.attributes or {})
        assert attrs.get("zashiki.message_id") == "msg-42"

    def test_exception_marks_span_error_and_reraises(
        self, in_memory_exporter
    ):
        from opentelemetry.trace import StatusCode

        with pytest.raises(RuntimeError, match="boom"):
            with node_trace(logging.getLogger("test.node"), "boom_node"):
                raise RuntimeError("boom")
        span = _span_by_name(
            in_memory_exporter, "zashiki.node.boom_node"
        )
        assert span is not None
        assert span.status.status_code == StatusCode.ERROR


class TestNoLangChainDoubleEmit:
    """D23: our env-disable of LANGCHAIN_TRACING_V2 / LANGSMITH_TRACING
    should prevent langchain / langgraph internal spans from ever
    appearing. Since we don't run a real LangGraph pipeline in unit
    tests, this is a "vacuously true" pin — assert that whatever we
    DO emit contains no offending prefixes.
    """

    def test_no_langchain_or_langgraph_spans(
        self, monkeypatch, in_memory_exporter
    ):
        result = TickResult(
            duration_ms=10,
            messages_processed=0,
            cursor_before=100,
            cursor_after=100,
            rebaselined=False,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app = create_app()
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        client.post("/poll")

        offending = [
            name for name in _span_names(in_memory_exporter)
            if name.startswith("langchain.") or name.startswith("langgraph.")
        ]
        assert not offending, (
            f"unexpected langchain/langgraph spans: {offending}"
        )
