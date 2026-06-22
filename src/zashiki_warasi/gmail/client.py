"""Gmail API wrapper.

Public methods return pydantic models (JSON-serializable) and are
documented for use both as Python APIs and as LangGraph LLM tools.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from email.utils import getaddresses
from typing import Iterable, Iterator

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import Resource, build
from googleapiclient.errors import HttpError

from zashiki_warasi.core.schemas import (
    AttachmentMeta,
    EmailMessage,
    ProfileInfo,
)
from zashiki_warasi.gmail.exceptions import (
    HistoryExpiredError,
    MessageNotFoundError,
)


class GmailClient:
    """Thin wrapper over the Gmail v1 REST API.

    Holds an authenticated `googleapiclient` service and exposes
    high-level methods that return parsed `EmailMessage` /
    `AttachmentMeta` / `ProfileInfo` models. Does not touch the
    database — persistence lives in the poller.
    """

    def __init__(self, credentials: Credentials) -> None:
        self._service: Resource = build(
            "gmail", "v1", credentials=credentials, cache_discovery=False
        )
        self._user_id = "me"

    # ---------- Tool-facing methods (also used internally) ----------

    def get_message(self, message_id: str) -> EmailMessage:
        """Fetch a single Gmail message by ID and return it parsed.

        Args:
            message_id: The Gmail message ID (e.g. from a search result
                or a history event).

        Returns:
            An `EmailMessage` with headers, decoded plain/HTML bodies,
            label IDs, and attachment metadata. Attachment bytes are NOT
            included — call `get_attachment` to download them.

        Raises:
            MessageNotFoundError: The message was deleted or is no
                longer accessible.
        """
        try:
            raw = (
                self._service.users()
                .messages()
                .get(userId=self._user_id, id=message_id, format="full")
                .execute()
            )
        except HttpError as exc:
            if exc.resp.status == 404:
                raise MessageNotFoundError(message_id) from exc
            raise
        return self._parse_message(raw)

    def get_attachment(self, message_id: str, attachment_id: str) -> bytes:
        """Download attachment bytes for a given message.

        Use the `attachment_id` from `EmailMessage.attachments`. Only
        call this when the agent has decided it needs the attachment
        contents — it can be large.

        Args:
            message_id: The owning message's Gmail ID.
            attachment_id: The attachment's ID from `AttachmentMeta`.

        Returns:
            Raw attachment bytes (caller is responsible for any further
            decoding such as PDF extraction).
        """
        raw = (
            self._service.users()
            .messages()
            .attachments()
            .get(
                userId=self._user_id,
                messageId=message_id,
                id=attachment_id,
            )
            .execute()
        )
        return base64.urlsafe_b64decode(raw["data"])

    # ---------- Infrastructure (used by poller) ----------

    def get_profile(self) -> ProfileInfo:
        """Return the authenticated user's email and current historyId.

        Used to baseline the history cursor on first run.
        """
        raw = (
            self._service.users()
            .getProfile(userId=self._user_id)
            .execute()
        )
        return ProfileInfo(
            email=raw["emailAddress"],
            history_id=int(raw["historyId"]),
        )

    def list_history(
        self,
        start_history_id: int,
        history_types: tuple[str, ...] = ("messageAdded",),
    ) -> Iterator[str]:
        """Yield message IDs added since `start_history_id`.

        Lazily paginates over Gmail's history.list. Yields each message
        ID exactly once across pages (de-duplicating within a single
        call).

        Args:
            start_history_id: The historyId from the last successful
                tick (or from `get_profile` on first run).
            history_types: Which history event types to subscribe to.
                Defaults to ("messageAdded",) — new messages only.

        Raises:
            HistoryExpiredError: `start_history_id` is older than
                Gmail's ~7-day retention window. Caller must re-baseline.
        """
        seen: set[str] = set()
        page_token: str | None = None
        history_api = self._service.users().history()
        while True:
            try:
                kwargs = {
                    "userId": self._user_id,
                    "startHistoryId": str(start_history_id),
                    "historyTypes": list(history_types),
                }
                if page_token:
                    kwargs["pageToken"] = page_token
                response = history_api.list(**kwargs).execute()
            except HttpError as exc:
                if exc.resp.status == 404:
                    raise HistoryExpiredError(start_history_id) from exc
                raise

            for record in response.get("history", []):
                for added in record.get("messagesAdded", []):
                    msg_id = added["message"]["id"]
                    if msg_id not in seen:
                        seen.add(msg_id)
                        yield msg_id

            page_token = response.get("nextPageToken")
            if not page_token:
                return

    # ---------- Parsing helpers ----------

    def _parse_message(self, raw: dict) -> EmailMessage:
        payload = raw.get("payload", {})
        headers = self._parse_headers(payload.get("headers", []))
        leaves = list(self._walk_parts(payload))
        body_plain, body_html = self._extract_bodies(leaves)
        attachments = self._extract_attachments(leaves)
        return EmailMessage(
            id=raw["id"],
            thread_id=raw["threadId"],
            history_id=int(raw["historyId"]),
            from_address=self._first_address(headers.get("from", "")),
            to_addresses=self._parse_addresses(headers.get("to", "")),
            cc_addresses=self._parse_addresses(headers.get("cc", "")),
            subject=headers.get("subject", ""),
            snippet=raw.get("snippet", ""),
            body_plain=body_plain,
            body_html=body_html,
            received_at=datetime.fromtimestamp(
                int(raw["internalDate"]) / 1000, tz=timezone.utc
            ),
            labels=list(raw.get("labelIds", [])),
            attachments=attachments,
            raw_headers=headers,
        )

    @staticmethod
    def _parse_headers(headers: list[dict]) -> dict[str, str]:
        """Gmail headers list -> case-insensitive lowercase-keyed dict.

        Duplicate header names are joined with ", " (last value wins for
        most headers, but joining preserves Received chains etc.).
        """
        out: dict[str, str] = {}
        for h in headers:
            key = h["name"].lower()
            value = h.get("value", "")
            if key in out:
                out[key] = f"{out[key]}, {value}"
            else:
                out[key] = value
        return out

    @classmethod
    def _walk_parts(cls, payload: dict) -> Iterator[dict]:
        """DFS over the payload tree yielding leaf parts."""
        parts = payload.get("parts")
        if not parts:
            yield payload
            return
        for part in parts:
            yield from cls._walk_parts(part)

    @staticmethod
    def _extract_bodies(
        leaves: Iterable[dict],
    ) -> tuple[str | None, str | None]:
        plain: str | None = None
        html: str | None = None
        for part in leaves:
            mime = part.get("mimeType", "")
            data = part.get("body", {}).get("data")
            if not data:
                continue
            if mime == "text/plain" and plain is None:
                plain = GmailClient._decode_body(data, part)
            elif mime == "text/html" and html is None:
                html = GmailClient._decode_body(data, part)
        return plain, html

    @staticmethod
    def _extract_attachments(leaves: Iterable[dict]) -> list[AttachmentMeta]:
        out: list[AttachmentMeta] = []
        for part in leaves:
            body = part.get("body", {})
            attachment_id = body.get("attachmentId")
            filename = part.get("filename") or ""
            if not attachment_id or not filename:
                continue
            out.append(
                AttachmentMeta(
                    attachment_id=attachment_id,
                    filename=filename,
                    mime_type=part.get("mimeType", "application/octet-stream"),
                    size=int(body.get("size", 0)),
                )
            )
        return out

    @staticmethod
    def _decode_body(data: str, part: dict) -> str:
        raw = base64.urlsafe_b64decode(data + "==")
        charset = GmailClient._charset_from_part(part) or "utf-8"
        return raw.decode(charset, errors="replace")

    @staticmethod
    def _charset_from_part(part: dict) -> str | None:
        for h in part.get("headers", []):
            if h.get("name", "").lower() == "content-type":
                for token in h.get("value", "").split(";"):
                    token = token.strip()
                    if token.lower().startswith("charset="):
                        return token.split("=", 1)[1].strip().strip('"')
        return None

    @staticmethod
    def _parse_addresses(raw: str) -> list[str]:
        if not raw:
            return []
        return [addr for _, addr in getaddresses([raw]) if addr]

    @classmethod
    def _first_address(cls, raw: str) -> str:
        addrs = cls._parse_addresses(raw)
        return addrs[0] if addrs else ""
