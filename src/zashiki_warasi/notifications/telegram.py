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
from zashiki_warasi.observability import api_call, telegram_send_total
from zashiki_warasi.observability.instrumentation import (
    observe_outcome,
    zashiki_span,
)


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

    def send_message(
        self,
        text: str,
        *,
        parse_mode: str = "HTML",
        reply_markup: dict | None = None,
    ) -> None:
        """POST sendMessage; raises TelegramError on non-2xx or ok=false.

        Emits `zashiki_telegram_send_total{outcome="success"}` on clean
        return; `outcome="error"` when any TelegramError raises. Wrapped
        in observe_outcome so callers that catch and log the exception
        (email_agent's notify node) still see the counter increment.

        `reply_markup` — optional Telegram Bot API reply_markup dict
        (e.g. an inline_keyboard with a copy_text button carrying the
        current trace_id). Passed through unchanged to the API; the
        sink doesn't validate its shape (that's Telegram's contract).
        See `_trace_markup.build_trace_copy_markup` for the current
        producer.
        """
        with zashiki_span(
            "notify.telegram",
            attributes={"messaging.system": "telegram"},
        ), observe_outcome(counter=telegram_send_total), api_call(
            "telegram", "send_message"
        ) as _ac:
            url = (
                f"{self._settings.api_base}/bot{self._settings.bot_token}"
                "/sendMessage"
            )
            payload: dict = {
                "chat_id": self._settings.chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            try:
                response = httpx.post(
                    url,
                    json=payload,
                    timeout=self._settings.timeout_seconds,
                )
            except httpx.HTTPError as exc:
                raise TelegramError(f"transport error: {exc}") from exc

            _ac.status_code = response.status_code
            if response.status_code >= 400:
                raise TelegramError(
                    f"HTTP {response.status_code}: {response.text}"
                )

            body = response.json()
            if not body.get("ok", False):
                raise TelegramError(
                    f"API rejected request: {body.get('description', body)}"
                )
