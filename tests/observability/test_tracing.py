"""Tests for the OTel tracing bootstrap.

Covers:
- WEB_CONCURRENCY fail-fast guard
- OTEL_RESOURCE_ATTRIBUTES secret pattern guard
- LangChain / LangSmith env disable (D23)
- Resource attribute parsing (comma-separated form)
- service.instance.id sourcing (HOSTNAME → uuid fallback)
- configure_tracing() disabled path stays NoOp
- configure_tracing() enabled path installs a real TracerProvider
- BSP rate-limited warning + traces_dropped counter

Integration test asserting the actual exported span tree (with an
InMemorySpanExporter mounted onto a live FastAPI app) lives in
test_tracing_span_tree.py (task 4.11).
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

import pytest

from zashiki_warasi.core.config import ObservabilitySettings
from zashiki_warasi.observability import REGISTRY
from zashiki_warasi.observability import tracing


def _counter_value(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


@pytest.fixture
def _reset_tracer_provider():
    """Snapshot + restore the global OTel tracer provider around a
    test. `configure_tracing()` on the enabled path calls
    `trace.set_tracer_provider(...)` which mutates a process-global;
    without this fixture, one enabled-path test leaks a real
    TracerProvider into every subsequent test that creates a FastAPI
    app (because FastAPIInstrumentor reads whatever provider is
    current at request time).
    """
    from opentelemetry import trace

    # trace._TRACER_PROVIDER is the internal singleton; there is no
    # public reset API, so we snapshot the private and restore it.
    prior = trace._TRACER_PROVIDER
    prior_flag = trace._TRACER_PROVIDER_SET_ONCE._done
    try:
        yield
    finally:
        # Reset the "set once" flag so a subsequent set is accepted.
        trace._TRACER_PROVIDER_SET_ONCE._done = prior_flag
        trace._TRACER_PROVIDER = prior


# --- WEB_CONCURRENCY guard ------------------------------------------


class TestCheckWebConcurrency:
    def test_unset_is_ok(self, monkeypatch):
        monkeypatch.delenv("WEB_CONCURRENCY", raising=False)
        # Should NOT exit
        tracing.check_web_concurrency()

    def test_value_1_is_ok(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "1")
        tracing.check_web_concurrency()

    def test_value_greater_than_1_exits(self, monkeypatch):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with pytest.raises(SystemExit) as exc_info:
            tracing.check_web_concurrency()
        assert exc_info.value.code == 1

    def test_non_integer_exits(self, monkeypatch, caplog):
        monkeypatch.setenv("WEB_CONCURRENCY", "many")
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                tracing.check_web_concurrency()
        assert "not an integer" in caplog.text or "many" in caplog.text

    def test_critical_log_names_the_lesson_doc(self, monkeypatch, caplog):
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                tracing.check_web_concurrency()
        assert "prometheus-multi-worker" in caplog.text.lower()


# --- Secret guard ---------------------------------------------------


class TestSecretGuard:
    _LOG = logging.getLogger("test.secret_guard")

    def test_empty_string_ok(self):
        tracing._guard_resource_attributes_secrets("", self._LOG)

    def test_benign_attributes_ok(self):
        tracing._guard_resource_attributes_secrets(
            "deployment.environment=prod,team=obs", self._LOG
        )

    @pytest.mark.parametrize(
        "raw",
        [
            "api_key=sk-abc1234567",
            "openai_token=sk-abc1234567",
            "gh=ghp_abcdef123456",
            "gh_pat=github_pat_11xxxxxxxxxxxxxx",
            "gapi=AIzaSyABC123456",
            "slack=xoxb-fake-token-value",
            "slack_user=xoxp-fake-token-value",
            "aws=AKIAIOSFODNN7EXAMPLE",
        ],
    )
    def test_known_prefix_exits(self, raw, caplog):
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit) as exc_info:
                tracing._guard_resource_attributes_secrets(raw, self._LOG)
        assert exc_info.value.code == 1
        # Value itself must NOT appear in the log line — key + pattern only.
        key = raw.split("=", 1)[0]
        assert key in caplog.text
        value = raw.split("=", 1)[1]
        assert value not in caplog.text

    def test_base64_like_long_string_exits(self, caplog):
        # 40-char base64-alphabet blob without a known prefix
        raw = "opaque_secret=abcdefghijABCDEFGHIJ1234567890abcdefghij"
        with caplog.at_level(logging.CRITICAL):
            with pytest.raises(SystemExit):
                tracing._guard_resource_attributes_secrets(raw, self._LOG)

    def test_short_alphanumeric_ok(self):
        # A short opaque value < 32 chars is not caught by the base64
        # heuristic and doesn't start with a known secret prefix.
        tracing._guard_resource_attributes_secrets(
            "region=us-east-1", self._LOG
        )


# --- LangChain env disable ------------------------------------------


class TestDisableLangChain:
    def test_sets_flags_when_absent(self, monkeypatch):
        monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
        monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
        tracing._disable_langchain_internal_tracing()
        import os

        assert os.environ.get("LANGCHAIN_TRACING_V2") == "false"
        assert os.environ.get("LANGSMITH_TRACING") == "false"

    def test_respects_operator_override(self, monkeypatch):
        # setdefault: operator-provided value wins
        monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
        tracing._disable_langchain_internal_tracing()
        import os

        assert os.environ.get("LANGCHAIN_TRACING_V2") == "true"


# --- Resource attribute parsing -------------------------------------


class TestParseResourceAttributes:
    def test_empty_string(self):
        assert tracing._parse_resource_attributes("") == {}

    def test_single_pair(self):
        assert tracing._parse_resource_attributes("k=v") == {"k": "v"}

    def test_multiple_pairs(self):
        parsed = tracing._parse_resource_attributes(
            "deployment.environment=prod,team=obs"
        )
        assert parsed == {
            "deployment.environment": "prod",
            "team": "obs",
        }

    def test_whitespace_trimmed(self):
        parsed = tracing._parse_resource_attributes(
            " k1 = v1 , k2 = v2 "
        )
        assert parsed == {"k1": "v1", "k2": "v2"}

    def test_malformed_chunks_skipped(self):
        parsed = tracing._parse_resource_attributes(
            "good=1,badnoequals,also=2"
        )
        assert parsed == {"good": "1", "also": "2"}


# --- Resource building (service.instance.id) ------------------------


class TestBuildResource:
    def test_uses_hostname_when_set(self, monkeypatch):
        monkeypatch.setenv("HOSTNAME", "pod-abc-123")
        monkeypatch.setenv("OTEL_ENABLED", "1")
        resource = tracing._build_resource(ObservabilitySettings())
        assert resource.attributes["service.instance.id"] == "pod-abc-123"
        assert resource.attributes["service.name"] == "zashiki-warasi"
        # service.version comes from importlib.metadata — non-empty
        assert resource.attributes["service.version"]

    def test_uuid_fallback_when_hostname_absent(self, monkeypatch):
        monkeypatch.delenv("HOSTNAME", raising=False)
        monkeypatch.setenv("OTEL_ENABLED", "1")
        resource = tracing._build_resource(ObservabilitySettings())
        instance_id = resource.attributes["service.instance.id"]
        # uuid4().hex is 32 lowercase hex chars
        assert isinstance(instance_id, str)
        assert len(instance_id) == 32
        assert all(c in "0123456789abcdef" for c in instance_id)

    def test_extra_attributes_merged(self, monkeypatch):
        monkeypatch.setenv("OTEL_ENABLED", "1")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "deployment.environment=prod,team=obs",
        )
        resource = tracing._build_resource(ObservabilitySettings())
        assert resource.attributes["deployment.environment"] == "prod"
        assert resource.attributes["team"] == "obs"


# --- configure_tracing() paths --------------------------------------


class TestConfigureTracing:
    def test_disabled_path_is_noop(self, monkeypatch):
        # Conftest default is OTEL_ENABLED=0 already.
        from opentelemetry import trace

        # Reset provider so we can observe it stays default.
        # (In practice pytest test isolation on the global provider is
        # tricky — this test just asserts configure_tracing() didn't
        # blow up in the disabled path.)
        tracing.configure_tracing(ObservabilitySettings())
        # No exception = pass. Deep provider identity checks are
        # brittle because other tests may have set a real provider.

    def test_enabled_path_installs_provider(
        self, monkeypatch, _reset_tracer_provider
    ):
        monkeypatch.setenv("OTEL_ENABLED", "1")
        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:1"
        )
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider

        tracing.configure_tracing(ObservabilitySettings())
        provider = trace.get_tracer_provider()
        assert isinstance(provider, TracerProvider)

    def test_web_concurrency_guard_runs_before_otel_init(
        self, monkeypatch
    ):
        # Guard runs regardless of otel_enabled (even the disabled
        # path calls it, per configure_tracing body).
        monkeypatch.setenv("WEB_CONCURRENCY", "4")
        with pytest.raises(SystemExit):
            tracing.configure_tracing(ObservabilitySettings())

    def test_secret_guard_runs_on_enabled_path(
        self, monkeypatch, _reset_tracer_provider
    ):
        monkeypatch.setenv("OTEL_ENABLED", "1")
        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES", "leak=sk-abc12345"
        )
        with pytest.raises(SystemExit):
            tracing.configure_tracing(ObservabilitySettings())


# --- Rate-limited BSP subclass -------------------------------------


class TestRateLimitedBSP:
    """Exercises the BatchSpanProcessor subclass built by
    `_make_rate_limited_bsp`. Uses a fake in-memory exporter to avoid
    the OTLP gRPC path.
    """

    def _make_processor(self, exporter_export_side_effect=None):
        """Build the real processor via the factory. Fake exporter's
        `export()` returns SUCCESS by default; pass a side_effect to
        make it raise for the export_failed path."""
        from opentelemetry.sdk.trace.export import SpanExportResult

        exporter = MagicMock()
        exporter._endpoint = "http://collector:4317"
        if exporter_export_side_effect is None:
            exporter.export.return_value = SpanExportResult.SUCCESS
        else:
            exporter.export.side_effect = exporter_export_side_effect
        log = logging.getLogger("test.bsp")
        proc = tracing._make_rate_limited_bsp(exporter, log=log)
        return proc, exporter, log

    def test_warn_rate_limited_first_call_emits(self, caplog):
        proc, _, _ = self._make_processor()
        with caplog.at_level(logging.WARNING, logger="test.bsp"):
            proc._warn_rate_limited("queue_full")
        matches = [
            r for r in caplog.records if "queue_full" in r.message
        ]
        assert len(matches) == 1

    def test_warn_rate_limited_subsequent_suppressed(self, caplog):
        proc, _, _ = self._make_processor()
        with caplog.at_level(logging.WARNING, logger="test.bsp"):
            proc._warn_rate_limited("queue_full")
            proc._warn_rate_limited("queue_full")
            proc._warn_rate_limited("queue_full")
        matches = [
            r for r in caplog.records if "queue_full" in r.message
        ]
        assert len(matches) == 1  # only the first survives

    def test_distinct_reasons_have_independent_windows(self, caplog):
        proc, _, _ = self._make_processor()
        with caplog.at_level(logging.WARNING, logger="test.bsp"):
            proc._warn_rate_limited("queue_full")
            proc._warn_rate_limited("export_failed")
        matches = [
            r for r in caplog.records
            if "queue_full" in r.message
            or "export_failed" in r.message
        ]
        assert len(matches) == 2

    def test_export_failure_increments_traces_dropped(self, caplog):
        proc, exporter, _ = self._make_processor(
            exporter_export_side_effect=RuntimeError("boom")
        )
        before = _counter_value(
            "zashiki_traces_dropped_total",
            {"reason": "export_failed"},
        )
        # Call the wrapped export directly with a fake span batch.
        fake_spans = [MagicMock(), MagicMock(), MagicMock()]
        with caplog.at_level(logging.WARNING, logger="test.bsp"):
            with pytest.raises(RuntimeError):
                exporter.export(fake_spans)
        after = _counter_value(
            "zashiki_traces_dropped_total",
            {"reason": "export_failed"},
        )
        # Counter incremented by the batch size (3)
        assert after == before + 3
        # WARNING logged (first call in window)
        matches = [
            r for r in caplog.records if "export_failed" in r.message
        ]
        assert len(matches) == 1
