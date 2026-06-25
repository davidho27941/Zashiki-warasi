"""Telegram Bot API client for outbound notifications.

Wraps the single endpoint we currently use (sendMessage). Non-2xx
responses raise so callers can decide whether to retry; the email
agent's notify node lets the exception propagate so LangGraph's
checkpoint keeps the analyze result and the next invoke resumes at
notify (no LLM re-call).
"""

from __future__ import annotations

import httpx

from zashiki_warasi.core.config import TelegramSettings


class TelegramError(Exception):
    """Telegram API returned non-success."""


class TelegramNotifier:
    def __init__(self, settings: TelegramSettings | None = None) -> None:
        self._settings = settings or TelegramSettings()
        if not self._settings.bot_token:
            raise ValueError(
                "TELEGRAM_BOT_TOKEN is not set; cannot construct "
                "TelegramNotifier."
            )
        if not self._settings.chat_id:
            raise ValueError(
                "TELEGRAM_CHAT_ID is not set; cannot construct "
                "TelegramNotifier."
            )

    def send_message(self, text: str, *, parse_mode: str = "HTML") -> None:
        """POST sendMessage; raises TelegramError on non-2xx or ok=false."""
        url = (
            f"{self._settings.api_base}/bot{self._settings.bot_token}"
            "/sendMessage"
        )
        payload = {
            "chat_id": self._settings.chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        try:
            response = httpx.post(
                url,
                json=payload,
                timeout=self._settings.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise TelegramError(f"transport error: {exc}") from exc

        if response.status_code >= 400:
            raise TelegramError(
                f"HTTP {response.status_code}: {response.text}"
            )

        body = response.json()
        if not body.get("ok", False):
            raise TelegramError(
                f"API rejected request: {body.get('description', body)}"
            )
