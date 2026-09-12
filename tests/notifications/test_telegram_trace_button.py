"""v1.3: Telegram "📋 Copy trace ID" inline button.

Covers the trace-copy affordance shipped by add-notify-trace-id-copy-button:
- `build_trace_copy_markup` output shape + input validation
- `TelegramNotifier.send_message(..., reply_markup=...)` passthrough
- End-to-end read from an OTel active span (integration-style)
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest
from opentelemetry.sdk.trace import TracerProvider

from zashiki_warasi.core.config import TelegramSettings
from zashiki_warasi.notifications._trace_markup import (
    build_trace_copy_markup,
)
from zashiki_warasi.notifications.telegram import TelegramNotifier


# ---------- helper: build_trace_copy_markup ----------


class TestBuildTraceCopyMarkup:
    def test_valid_trace_id_returns_inline_keyboard(self):
        trace_id = "f8c75417404f3f7d6be7cccf5d72924b"
        markup = build_trace_copy_markup(trace_id)
        assert markup == {
            "inline_keyboard": [
                [
                    {
                        "text": "📋 Copy trace ID",
                        "copy_text": {"text": trace_id},
                    }
                ]
            ]
        }

    def test_none_returns_none(self):
        assert build_trace_copy_markup(None) is None

    def test_empty_string_returns_none(self):
        assert build_trace_copy_markup("") is None

    def test_all_zero_sentinel_returns_none(self):
        # NoOpTracerProvider returns 0 for span context trace_id;
        # formatted as 32-hex that's the all-zero string.
        assert build_trace_copy_markup("0" * 32) is None

    def test_wrong_length_returns_none(self):
        assert build_trace_copy_markup("abc") is None
        assert build_trace_copy_markup("a" * 31) is None
        assert build_trace_copy_markup("a" * 33) is None

    def test_non_hex_returns_none(self):
        # 32 chars but with non-hex characters
        assert build_trace_copy_markup("g" * 32) is None
        assert build_trace_copy_markup("z" + "a" * 31) is None

    def test_uppercase_hex_returns_none(self):
        # Contract is lowercase hex; uppercase = malformed input.
        assert build_trace_copy_markup("F" * 32) is None


# ---------- TelegramNotifier: reply_markup passthrough ----------


def _settings() -> TelegramSettings:
    return TelegramSettings(
        bot_token="123:abc",
        chat_id="-100",
        api_base="https://example.test",
        timeout_seconds=5.0,
    )


def _ok_response() -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = {"ok": True, "result": {}}
    response.text = ""
    return response


class TestReplyMarkupPassthrough:
    def test_default_send_has_no_reply_markup_key(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message("hi")

        payload = post.call_args.kwargs["json"]
        assert "reply_markup" not in payload

    def test_reply_markup_none_omits_key(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message(
            "hi", reply_markup=None
        )

        payload = post.call_args.kwargs["json"]
        assert "reply_markup" not in payload

    def test_reply_markup_passed_through_verbatim(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        markup = {
            "inline_keyboard": [
                [
                    {
                        "text": "📋 Copy trace ID",
                        "copy_text": {"text": "a" * 32},
                    }
                ]
            ]
        }
        TelegramNotifier(_settings()).send_message(
            "hi", reply_markup=markup
        )

        payload = post.call_args.kwargs["json"]
        assert payload["reply_markup"] == markup

    def test_reply_markup_arbitrary_dict_not_validated(self, monkeypatch):
        # Sink SHALL NOT introspect the dict — passes through whatever.
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        weird_markup = {"anything": {"nested": [1, 2, 3]}}
        TelegramNotifier(_settings()).send_message(
            "hi", reply_markup=weird_markup
        )

        assert (
            post.call_args.kwargs["json"]["reply_markup"] == weird_markup
        )


# ---------- integration: read trace_id from active span ----------


class TestEmailAgentTraceContextIntegration:
    """Read active span trace_id via the email_agent module-level helper.

    Uses `start_as_current_span` on a local TracerProvider — no global
    provider mutation, so no cross-test state leakage.
    """

    def test_notify_inside_span_passes_trace_id_to_telegram(self):
        from zashiki_warasi.agents.email_agent import (
            _current_trace_copy_markup,
        )

        tracer = TracerProvider().get_tracer("test")
        with tracer.start_as_current_span("test-span") as span:
            expected_trace_id = f"{span.get_span_context().trace_id:032x}"
            markup = _current_trace_copy_markup()

        assert markup is not None
        button = markup["inline_keyboard"][0][0]
        assert button["text"] == "📋 Copy trace ID"
        assert button["copy_text"]["text"] == expected_trace_id

    def test_notify_outside_span_returns_none(self):
        """No active span → markup is None → sink sends text-only."""
        from zashiki_warasi.agents.email_agent import (
            _current_trace_copy_markup,
        )

        # No `start_as_current_span` in scope; default NoOp context.
        assert _current_trace_copy_markup() is None
