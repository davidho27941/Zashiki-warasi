"""TelegramNotifier: URL construction, payload shape, error mapping."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from zashiki_warasi.core.config import TelegramSettings
from zashiki_warasi.notifications.telegram import (
    TelegramError,
    TelegramNotifier,
)


def _settings(**overrides) -> TelegramSettings:
    base = dict(
        bot_token="123:abc",
        chat_id="-100",
        api_base="https://example.test",
        timeout_seconds=5.0,
    )
    base.update(overrides)
    return TelegramSettings(**base)


def _ok_response(payload: dict | None = None) -> MagicMock:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    response.json.return_value = payload or {"ok": True, "result": {}}
    response.text = ""
    return response


# ---------- construction ----------


class TestConstruction:
    def test_rejects_empty_bot_token(self):
        with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN"):
            TelegramNotifier(_settings(bot_token=""))

    def test_rejects_empty_chat_id(self):
        with pytest.raises(ValueError, match="TELEGRAM_CHAT_ID"):
            TelegramNotifier(_settings(chat_id=""))

    def test_uses_settings_from_env_when_none_passed(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "env:tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "999")
        notifier = TelegramNotifier()  # no settings passed
        assert notifier is not None


# ---------- send_message: request shape ----------


class TestRequest:
    def test_posts_to_correct_url(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message("hi")

        url = post.call_args.args[0]
        assert url == "https://example.test/bot123:abc/sendMessage"

    def test_payload_contains_chat_id_and_text(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message("hello world")

        payload = post.call_args.kwargs["json"]
        assert payload["chat_id"] == "-100"
        assert payload["text"] == "hello world"

    def test_default_parse_mode_is_html(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message("hi")

        assert post.call_args.kwargs["json"]["parse_mode"] == "HTML"

    def test_parse_mode_can_be_overridden(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message(
            "hi", parse_mode="MarkdownV2"
        )

        assert (
            post.call_args.kwargs["json"]["parse_mode"] == "MarkdownV2"
        )

    def test_web_page_preview_disabled(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings()).send_message("hi")

        assert (
            post.call_args.kwargs["json"]["disable_web_page_preview"]
            is True
        )

    def test_timeout_passed_from_settings(self, monkeypatch):
        post = MagicMock(return_value=_ok_response())
        monkeypatch.setattr(httpx, "post", post)

        TelegramNotifier(_settings(timeout_seconds=42.0)).send_message(
            "hi"
        )

        assert post.call_args.kwargs["timeout"] == 42.0


# ---------- error handling ----------


class TestErrorMapping:
    def test_transport_error_raises_telegram_error(self, monkeypatch):
        post = MagicMock(
            side_effect=httpx.ConnectError("DNS failure")
        )
        monkeypatch.setattr(httpx, "post", post)

        with pytest.raises(TelegramError, match="transport error"):
            TelegramNotifier(_settings()).send_message("hi")

    def test_http_4xx_raises_telegram_error(self, monkeypatch):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 401
        response.text = "Unauthorized"
        monkeypatch.setattr(
            httpx, "post", MagicMock(return_value=response)
        )

        with pytest.raises(TelegramError, match="HTTP 401"):
            TelegramNotifier(_settings()).send_message("hi")

    def test_http_5xx_raises_telegram_error(self, monkeypatch):
        response = MagicMock(spec=httpx.Response)
        response.status_code = 502
        response.text = "Bad Gateway"
        monkeypatch.setattr(
            httpx, "post", MagicMock(return_value=response)
        )

        with pytest.raises(TelegramError, match="HTTP 502"):
            TelegramNotifier(_settings()).send_message("hi")

    def test_ok_false_raises_telegram_error(self, monkeypatch):
        # Telegram occasionally returns 200 with {"ok": false, ...}
        response = MagicMock(spec=httpx.Response)
        response.status_code = 200
        response.json.return_value = {
            "ok": False,
            "description": "chat not found",
        }
        monkeypatch.setattr(
            httpx, "post", MagicMock(return_value=response)
        )

        with pytest.raises(TelegramError, match="chat not found"):
            TelegramNotifier(_settings()).send_message("hi")

    def test_success_does_not_raise(self, monkeypatch):
        monkeypatch.setattr(
            httpx, "post", MagicMock(return_value=_ok_response())
        )

        TelegramNotifier(_settings()).send_message("hi")  # no raise
