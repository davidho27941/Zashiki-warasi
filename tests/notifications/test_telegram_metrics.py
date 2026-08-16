"""Metric-emission tests for the Telegram notifier — verifies
zashiki_telegram_send_total{outcome} increments on both success and
error paths.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from zashiki_warasi.core.config import TelegramSettings
from zashiki_warasi.notifications.telegram import (
    TelegramError,
    TelegramNotifier,
)
from zashiki_warasi.observability import REGISTRY


def _counter_value(name: str, labels: dict[str, str]) -> float:
    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


@pytest.fixture
def notifier():
    return TelegramNotifier(
        settings=TelegramSettings(
            bot_token="fake-bot-token",
            chat_id="12345",
        )
    )


class TestTelegramSendMetrics:
    def test_success_increments_success_counter(self, notifier):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {"ok": True}
        with patch(
            "zashiki_warasi.notifications.telegram.httpx.post",
            return_value=response,
        ):
            before = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "success"},
            )
            notifier.send_message("hello")
            after = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "success"},
            )
        assert after == before + 1

    def test_http_error_increments_error_counter(self, notifier):
        response = MagicMock()
        response.status_code = 500
        response.text = "internal server error"
        with patch(
            "zashiki_warasi.notifications.telegram.httpx.post",
            return_value=response,
        ):
            before = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
            with pytest.raises(TelegramError):
                notifier.send_message("hello")
            after = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
        assert after == before + 1

    def test_transport_error_increments_error_counter(self, notifier):
        with patch(
            "zashiki_warasi.notifications.telegram.httpx.post",
            side_effect=httpx.ConnectError("connection refused"),
        ):
            before = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
            with pytest.raises(TelegramError):
                notifier.send_message("hello")
            after = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
        assert after == before + 1

    def test_api_rejects_ok_false_increments_error_counter(self, notifier):
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "ok": False,
            "description": "chat not found",
        }
        with patch(
            "zashiki_warasi.notifications.telegram.httpx.post",
            return_value=response,
        ):
            before = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
            with pytest.raises(TelegramError):
                notifier.send_message("hello")
            after = _counter_value(
                "zashiki_telegram_send_total",
                {"outcome": "error"},
            )
        assert after == before + 1
