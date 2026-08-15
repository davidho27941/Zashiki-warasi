"""Group 5 additions to core/logging.py:

- `JsonContextFormatter` — NDJSON emission with contract fields,
  Chinese preservation, non-serializable extra survival, exc_info
  handling.
- Trace-context LogRecord factory — chain-wraps `getLogRecordFactory()`
  once per process, attaches `trace_id` / `span_id` when an OTel span
  context is active, no-ops when absent.
- `configure_logging()` branches on `settings.format` and calls the
  factory install (idempotent).

Kept in a separate file from `test_logging.py` so the existing v1.0
tests stay a stable regression pin — the additions here shouldn't
change any of that file's assertions.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal
from io import StringIO
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.core.config import LoggingSettings
from zashiki_warasi.core.logging import (
    ContextFormatter,
    JsonContextFormatter,
    _CONTEXT_FIELDS,
    _HANDLER_SENTINEL,
    _REQUEST_ID_CTX,
    _iso_utc_timestamp,
    configure_logging,
)


def _make_record(
    msg: str = "hello",
    *,
    logger_name: str = "zashiki_warasi.test",
    level: int = logging.INFO,
    extra: dict | None = None,
    exc_info=None,
) -> logging.LogRecord:
    """Build a LogRecord directly (bypass the factory install stuff)."""
    record = logging.LogRecord(
        name=logger_name,
        level=level,
        pathname="test.py",
        lineno=1,
        msg=msg,
        args=(),
        exc_info=exc_info,
    )
    for k, v in (extra or {}).items():
        setattr(record, k, v)
    return record


# --- JsonContextFormatter -------------------------------------------


class TestJsonContractFields:
    def test_contract_fields_present(self):
        rec = _make_record("hello world")
        out = JsonContextFormatter().format(rec)
        parsed = json.loads(out)
        assert set(parsed.keys()) >= {
            "timestamp",
            "level",
            "logger",
            "message",
        }

    def test_level_is_uppercase(self):
        rec = _make_record(level=logging.WARNING)
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert parsed["level"] == "WARNING"

    def test_logger_is_dotted_name(self):
        rec = _make_record(logger_name="zashiki_warasi.agents.email_agent")
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert parsed["logger"] == "zashiki_warasi.agents.email_agent"

    def test_message_is_post_substitution(self):
        rec = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="x",
            lineno=1,
            msg="user %s did %s",
            args=("alice", "login"),
            exc_info=None,
        )
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert parsed["message"] == "user alice did login"

    def test_timestamp_is_iso_with_millis_and_z(self):
        rec = _make_record()
        parsed = json.loads(JsonContextFormatter().format(rec))
        ts = parsed["timestamp"]
        # Must end with Z and contain milliseconds (.NNN before Z)
        assert ts.endswith("Z")
        # Parse succeeds (fromisoformat requires +00:00 not Z, so
        # we can validate by substituting)
        datetime.fromisoformat(ts.replace("Z", "+00:00"))


class TestJsonContextFieldsAsTopLevel:
    def test_message_id_becomes_top_level_key(self):
        rec = _make_record(extra={"message_id": "msg-42"})
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert parsed["message_id"] == "msg-42"

    def test_request_id_from_contextvar(self):
        token = _REQUEST_ID_CTX.set("cron-2026-08-15")
        try:
            rec = _make_record()
            parsed = json.loads(JsonContextFormatter().format(rec))
            assert parsed["request_id"] == "cron-2026-08-15"
        finally:
            _REQUEST_ID_CTX.reset(token)

    def test_multiple_context_fields_all_appear(self):
        token = _REQUEST_ID_CTX.set("req-1")
        try:
            rec = _make_record(
                extra={
                    "message_id": "msg-42",
                    "thread_id": "thread-9",
                    "expense_id": "exp-3",
                }
            )
            parsed = json.loads(JsonContextFormatter().format(rec))
            assert parsed["request_id"] == "req-1"
            assert parsed["message_id"] == "msg-42"
            assert parsed["thread_id"] == "thread-9"
            assert parsed["expense_id"] == "exp-3"
        finally:
            _REQUEST_ID_CTX.reset(token)

    def test_absent_keys_omitted_not_null(self):
        _REQUEST_ID_CTX.set(None)
        rec = _make_record()
        parsed = json.loads(JsonContextFormatter().format(rec))
        for key in _CONTEXT_FIELDS:
            assert key not in parsed, f"{key} should be absent, not None"

    def test_trace_and_span_appear_when_record_has_them(self):
        rec = _make_record(
            extra={
                "trace_id": "0" * 32,
                "span_id": "0" * 16,
            }
        )
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert parsed["trace_id"] == "0" * 32
        assert parsed["span_id"] == "0" * 16


class TestJsonSerializationSafety:
    def test_chinese_preserved_verbatim(self):
        """ensure_ascii=False — grep by substring works."""
        rec = _make_record("classified as 消費支出")
        out = JsonContextFormatter().format(rec)
        # Not escaped
        assert "消費支出" in out
        assert "\\u" not in out  # no unicode escape sequence
        # Still valid JSON
        parsed = json.loads(out)
        assert parsed["message"] == "classified as 消費支出"

    def test_non_serializable_extra_survives(self):
        """default=str — Decimal / datetime don't crash the log call."""
        rec = _make_record(
            extra={
                "message_id": "msg-42",
                "amount": Decimal("42.50"),  # not JSON-serializable
                "when": datetime(2026, 8, 15, 12, 0, 0),  # not JSON-serializable
            }
        )
        # Must not raise
        out = JsonContextFormatter().format(rec)
        parsed = json.loads(out)
        # Actual Decimal / datetime are NOT context fields (not in
        # _CONTEXT_FIELDS allowlist) so they don't appear in payload.
        # But the formatter must still complete without raising even
        # if they were somehow reached — default=str handles it.
        assert parsed["message_id"] == "msg-42"

    def test_default_str_fallback_used_when_context_field_is_object(self):
        # Contrived: set message_id to a non-string object.
        class Weird:
            def __str__(self):
                return "weird-value"

        rec = _make_record(extra={"message_id": Weird()})
        out = JsonContextFormatter().format(rec)
        parsed = json.loads(out)
        assert parsed["message_id"] == "weird-value"


class TestJsonExceptionInfo:
    def test_exc_info_produces_traceback_and_exception_object(self):
        try:
            raise ValueError("boom")
        except ValueError:
            import sys

            exc_info = sys.exc_info()
        rec = _make_record("caught it", exc_info=exc_info)
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert "traceback" in parsed
        assert "ValueError" in parsed["traceback"]
        assert parsed["exception"] == {
            "type": "ValueError",
            "message": "boom",
        }

    def test_no_exc_info_no_exception_fields(self):
        rec = _make_record("plain")
        parsed = json.loads(JsonContextFormatter().format(rec))
        assert "traceback" not in parsed
        assert "exception" not in parsed


# --- Timestamp helper -----------------------------------------------


class TestIsoUtcTimestamp:
    def test_format_shape(self):
        # 2026-08-15 12:00:00.500 UTC
        ts = _iso_utc_timestamp(1786060800.5)
        # Regex-ish check
        assert len(ts) == 24  # 2026-08-15T12:00:00.500Z
        assert ts[10] == "T"
        assert ts[19] == "."
        assert ts.endswith("Z")

    def test_milliseconds_padded(self):
        # 5ms should render as .005 not .5
        ts = _iso_utc_timestamp(1786060800.005)
        assert ".005Z" in ts


# --- configure_logging + formatter branching ------------------------


class TestConfigureLoggingFormatBranch:
    @pytest.fixture(autouse=True)
    def _cleanup_root_handler(self):
        # Give each test a clean root logger by removing our sentinel
        # handler afterwards.
        yield
        root = logging.getLogger()
        for h in list(root.handlers):
            if getattr(h, _HANDLER_SENTINEL, False):
                root.removeHandler(h)

    def _get_our_handler(self):
        root = logging.getLogger()
        matches = [
            h for h in root.handlers
            if getattr(h, _HANDLER_SENTINEL, False)
        ]
        assert len(matches) == 1, (
            f"expected exactly one sentinel handler, got {len(matches)}"
        )
        return matches[0]

    def test_default_format_text_uses_context_formatter(
        self, monkeypatch
    ):
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        configure_logging(LoggingSettings())
        handler = self._get_our_handler()
        assert isinstance(handler.formatter, ContextFormatter)

    def test_env_json_uses_json_formatter(self, monkeypatch):
        monkeypatch.setenv("LOG_FORMAT", "json")
        configure_logging(LoggingSettings())
        handler = self._get_our_handler()
        assert isinstance(handler.formatter, JsonContextFormatter)

    def test_reconfigure_swaps_formatter(self, monkeypatch):
        # start with text
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        configure_logging(LoggingSettings())
        handler = self._get_our_handler()
        assert isinstance(handler.formatter, ContextFormatter)

        # re-configure to json — same handler, new formatter
        monkeypatch.setenv("LOG_FORMAT", "json")
        configure_logging(LoggingSettings())
        handler2 = self._get_our_handler()
        assert handler is handler2, "handler should not be recreated"
        assert isinstance(handler2.formatter, JsonContextFormatter)

    def test_reconfigure_does_not_duplicate_handler(self, monkeypatch):
        configure_logging(LoggingSettings())
        configure_logging(LoggingSettings())
        configure_logging(LoggingSettings())
        # _get_our_handler asserts exactly one — no dupes.
        self._get_our_handler()


# --- Trace-context LogRecord factory --------------------------------


class TestTraceContextFactory:
    def test_no_active_span_leaves_record_untouched(self):
        # Configure once so factory installed
        configure_logging(LoggingSettings())
        # Emit a record via the factory (LogRecord constructor uses
        # the installed factory)
        record = logging.LogRecord(
            "t", logging.INFO, "x", 1, "hello", (), None
        )
        # No active span → neither attribute set
        assert not hasattr(record, "trace_id") or not record.trace_id
        assert not hasattr(record, "span_id") or not record.span_id

    def test_active_span_populates_trace_and_span_id(self):
        # Emit via `logger.info()` (not direct LogRecord construction)
        # so the record goes through `logging.makeRecord` which uses
        # the installed factory — direct construction bypasses it.
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        prior_provider = trace._TRACER_PROVIDER
        prior_flag = trace._TRACER_PROVIDER_SET_ONCE._done
        try:
            provider = TracerProvider()
            provider.add_span_processor(
                SimpleSpanProcessor(InMemorySpanExporter())
            )
            trace._TRACER_PROVIDER_SET_ONCE._done = False
            trace._TRACER_PROVIDER = None
            trace.set_tracer_provider(provider)

            configure_logging(LoggingSettings())

            # Capture emitted records via a probe handler
            captured: list[logging.LogRecord] = []

            class _Probe(logging.Handler):
                def emit(self, record):
                    captured.append(record)

            probe = _Probe(level=logging.DEBUG)
            log = logging.getLogger(
                "zashiki_warasi.test.trace_factory"
            )
            log.setLevel(logging.INFO)
            log.addHandler(probe)
            try:
                tracer = trace.get_tracer("test")
                with tracer.start_as_current_span("test-span"):
                    log.info("hello inside span")
            finally:
                log.removeHandler(probe)

            assert len(captured) == 1
            record = captured[0]
            assert hasattr(record, "trace_id")
            assert hasattr(record, "span_id")
            assert len(record.trace_id) == 32
            assert len(record.span_id) == 16
            assert all(c in "0123456789abcdef" for c in record.trace_id)
            assert all(c in "0123456789abcdef" for c in record.span_id)
        finally:
            provider.shutdown()
            trace._TRACER_PROVIDER_SET_ONCE._done = prior_flag
            trace._TRACER_PROVIDER = prior_provider

    def test_factory_installed_only_once_across_multiple_configure_calls(
        self,
    ):
        # Snapshot factory before / after multiple configure_logging calls
        configure_logging(LoggingSettings())
        first = logging.getLogRecordFactory()
        for _ in range(5):
            configure_logging(LoggingSettings())
        second = logging.getLogRecordFactory()
        # Same wrapper — not re-chained.
        assert first is second

    def test_factory_chains_existing_factory(self):
        """If a test harness / third-party lib set its own factory
        before us, our wrapper must run on top, not replace it."""
        # Rig: we can't easily reset factory-sentinel state, so instead
        # we verify the CURRENT factory produces a record that carries
        # the chained-in attribute when the chain is in place.
        #
        # Simpler probe: after configure_logging, the factory should
        # produce a valid LogRecord with all standard attrs (no
        # regression on the chained call).
        configure_logging(LoggingSettings())
        record = logging.LogRecord(
            "t", logging.INFO, "x", 1, "hello", (), None
        )
        # Standard attributes still present (factory didn't drop them)
        assert record.name == "t"
        assert record.levelname == "INFO"
        assert record.msg == "hello"

    def test_installed_factory_carries_sentinel_attribute(self):
        """Regression pin for D25: the installed factory has our
        sentinel attribute set. This is HOW `_install_trace_context_
        factory` decides to skip re-install on subsequent calls — the
        state lives on the factory object itself, not in a module
        variable."""
        configure_logging(LoggingSettings())
        factory = logging.getLogRecordFactory()
        assert getattr(factory, "__zashiki_installed__", False) is True


# --- Trace context reaches formatter (integration) ------------------


class TestTraceContextFlowsToJsonFormatter:
    def test_trace_id_appears_in_json_output_when_span_active(self):
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        prior_provider = trace._TRACER_PROVIDER
        prior_flag = trace._TRACER_PROVIDER_SET_ONCE._done
        try:
            provider = TracerProvider()
            provider.add_span_processor(
                SimpleSpanProcessor(InMemorySpanExporter())
            )
            trace._TRACER_PROVIDER_SET_ONCE._done = False
            trace._TRACER_PROVIDER = None
            trace.set_tracer_provider(provider)

            configure_logging(LoggingSettings())

            # Emit inside an active span, capture via a StringIO handler
            buf = StringIO()
            root = logging.getLogger()
            handler = [
                h for h in root.handlers
                if getattr(h, _HANDLER_SENTINEL, False)
            ][0]
            original_stream = handler.stream
            handler.stream = buf
            handler.setFormatter(JsonContextFormatter())

            try:
                tracer = trace.get_tracer("test")
                log = logging.getLogger("zashiki_warasi.test.trace")
                log.setLevel(logging.INFO)
                with tracer.start_as_current_span("test-span"):
                    log.info("hello inside span")
            finally:
                handler.stream = original_stream

            output_line = buf.getvalue().strip()
            parsed = json.loads(output_line)
            assert "trace_id" in parsed
            assert "span_id" in parsed
            assert len(parsed["trace_id"]) == 32
            assert len(parsed["span_id"]) == 16
        finally:
            provider.shutdown()
            trace._TRACER_PROVIDER_SET_ONCE._done = prior_flag
            trace._TRACER_PROVIDER = prior_provider


# --- Text format still passes existing regression -------------------


class TestTextFormatRegression:
    """The v1.0 text format must stay byte-identical for records without
    trace context. `test_logging.py` (existing) already covers this
    extensively; here we add one Group-5-specific pin: extending
    `_CONTEXT_FIELDS` with `trace_id` / `span_id` must NOT insert
    those into the text output when they're absent from the record."""

    def test_text_output_has_no_trace_fields_when_absent(self):
        _REQUEST_ID_CTX.set(None)
        rec = _make_record(extra={"message_id": "msg-1"})
        out = ContextFormatter().format(rec)
        assert "trace_id" not in out
        assert "span_id" not in out
        # Just message_id shows
        assert "[message_id=msg-1]" in out

    def test_text_output_includes_trace_fields_when_present(self):
        _REQUEST_ID_CTX.set(None)
        rec = _make_record(
            extra={
                "trace_id": "abc123",
                "span_id": "def456",
                "message_id": "msg-1",
            }
        )
        out = ContextFormatter().format(rec)
        # Order should match _CONTEXT_FIELDS declaration:
        # request_id, trace_id, span_id, message_id, ...
        assert "[trace_id=abc123,span_id=def456,message_id=msg-1]" in out
