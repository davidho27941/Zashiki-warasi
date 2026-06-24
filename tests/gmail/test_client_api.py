"""GmailClient API surface: HTTP-facing methods and error translation.

Parsing of `messages.get` payloads is covered in test_client_parsing.py;
these tests focus on the public methods' interaction with the
googleapiclient service (mocked), including the 404 -> custom exception
translation that the poller relies on.
"""

from __future__ import annotations

import base64
from unittest.mock import MagicMock

import pytest
from googleapiclient.errors import HttpError

from zashiki_warasi.core.schemas import ProfileInfo
from zashiki_warasi.gmail.client import GmailClient
from zashiki_warasi.gmail.exceptions import (
    HistoryExpiredError,
    MessageNotFoundError,
)


def _http_error(status: int) -> HttpError:
    resp = MagicMock()
    resp.status = status
    resp.reason = "test"
    return HttpError(resp=resp, content=b'{"error": {}}')


def _make_client(service: MagicMock) -> GmailClient:
    """Bypass __init__ so we don't need real Credentials."""
    client = GmailClient.__new__(GmailClient)
    client._service = service
    client._user_id = "me"
    return client


# --- get_profile ---


class TestGetProfile:
    def test_returns_profile_info_from_api_response(self):
        service = MagicMock()
        service.users().getProfile().execute.return_value = {
            "emailAddress": "alice@example.com",
            "messagesTotal": 1234,
            "historyId": "9876",
        }
        client = _make_client(service)

        profile = client.get_profile()

        assert isinstance(profile, ProfileInfo)
        assert profile.email == "alice@example.com"
        assert profile.history_id == 9876

    def test_passes_user_id_me(self):
        service = MagicMock()
        service.users().getProfile().execute.return_value = {
            "emailAddress": "a@x", "historyId": "1"
        }
        client = _make_client(service)

        client.get_profile()

        # Check getProfile was called with userId="me" at least once
        calls = service.users().getProfile.call_args_list
        assert any(c.kwargs.get("userId") == "me" for c in calls)


# --- get_message ---


class TestGetMessage:
    def _minimal_message_payload(self, msg_id: str = "abc") -> dict:
        return {
            "id": msg_id,
            "threadId": "t1",
            "historyId": "100",
            "internalDate": "1700000000000",
            "labelIds": ["INBOX"],
            "snippet": "snip",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "a@x"},
                    {"name": "Subject", "value": "hi"},
                ],
                "body": {"data": "aGVsbG8", "size": 5},
            },
        }

    def test_returns_parsed_email_message(self):
        service = MagicMock()
        service.users().messages().get().execute.return_value = (
            self._minimal_message_payload("msg-1")
        )
        client = _make_client(service)

        result = client.get_message("msg-1")

        assert result.id == "msg-1"
        assert result.subject == "hi"
        assert result.from_address == "a@x"

    def test_requests_full_format(self):
        service = MagicMock()
        service.users().messages().get().execute.return_value = (
            self._minimal_message_payload()
        )
        client = _make_client(service)

        client.get_message("msg-1")

        # Find the .get() call that took the actual id and format kwargs
        get_calls = service.users().messages().get.call_args_list
        full_call = next(c for c in get_calls if c.kwargs.get("id") == "msg-1")
        assert full_call.kwargs["format"] == "full"
        assert full_call.kwargs["userId"] == "me"

    def test_raises_message_not_found_on_404(self):
        service = MagicMock()
        service.users().messages().get().execute.side_effect = _http_error(404)
        client = _make_client(service)

        with pytest.raises(MessageNotFoundError) as exc_info:
            client.get_message("deleted-id")

        assert "deleted-id" in str(exc_info.value)

    def test_propagates_non_404_http_errors(self):
        service = MagicMock()
        service.users().messages().get().execute.side_effect = _http_error(500)
        client = _make_client(service)

        with pytest.raises(HttpError):
            client.get_message("some-id")


# --- get_attachment ---


class TestGetAttachment:
    def test_decodes_base64url_data(self):
        payload = b"hello attachment bytes"
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        service = MagicMock()
        service.users().messages().attachments().get().execute.return_value = {
            "data": encoded,
            "size": len(payload),
        }
        client = _make_client(service)

        result = client.get_attachment("msg-1", "att-1")

        assert result == payload

    def test_handles_url_safe_chars(self):
        # bytes that produce - and _ in base64url
        payload = bytes([255, 254, 253, 252])
        encoded = base64.urlsafe_b64encode(payload).decode().rstrip("=")
        assert "-" in encoded or "_" in encoded  # sanity check
        service = MagicMock()
        service.users().messages().attachments().get().execute.return_value = {
            "data": encoded,
            "size": len(payload),
        }
        client = _make_client(service)

        assert client.get_attachment("m", "a") == payload

    def test_passes_message_and_attachment_ids(self):
        service = MagicMock()
        service.users().messages().attachments().get().execute.return_value = {
            "data": ""
        }
        client = _make_client(service)

        client.get_attachment("msg-X", "att-Y")

        get_calls = service.users().messages().attachments().get.call_args_list
        target = next(
            c for c in get_calls if c.kwargs.get("messageId") == "msg-X"
        )
        assert target.kwargs["id"] == "att-Y"
        assert target.kwargs["userId"] == "me"


# --- list_history ---


class _HistoryPageBuilder:
    """Helper to build mock responses for users().history().list()."""

    def __init__(self) -> None:
        self._pages: list[dict] = []

    def page(
        self,
        message_ids: list[str],
        *,
        next_page_token: str | None = None,
    ) -> _HistoryPageBuilder:
        record = {
            "id": "rec",
            "messagesAdded": [
                {"message": {"id": mid, "threadId": "t"}} for mid in message_ids
            ],
        }
        page = {"history": [record]}
        if next_page_token:
            page["nextPageToken"] = next_page_token
        self._pages.append(page)
        return self

    def build(self) -> MagicMock:
        service = MagicMock()
        history_api = service.users().history()
        # Each call to list(...) returns a request whose execute() pops
        # the next prepared page in order.
        history_api.list.side_effect = lambda **kwargs: MagicMock(
            execute=MagicMock(return_value=self._pages.pop(0))
        )
        return service


class TestListHistory:
    def test_yields_message_ids_from_single_page(self):
        service = _HistoryPageBuilder().page(["m1", "m2", "m3"]).build()
        client = _make_client(service)

        result = list(client.list_history(start_history_id=100))

        assert result == ["m1", "m2", "m3"]

    def test_paginates_through_next_page_token(self):
        service = (
            _HistoryPageBuilder()
            .page(["m1", "m2"], next_page_token="tok-2")
            .page(["m3"])
            .build()
        )
        client = _make_client(service)

        result = list(client.list_history(start_history_id=100))

        assert result == ["m1", "m2", "m3"]

    def test_deduplicates_within_same_call(self):
        # Same message id appears in two pages — should yield once.
        service = (
            _HistoryPageBuilder()
            .page(["m1", "m2"], next_page_token="t")
            .page(["m2", "m3"])
            .build()
        )
        client = _make_client(service)

        result = list(client.list_history(start_history_id=100))

        assert result == ["m1", "m2", "m3"]

    def test_empty_response_yields_nothing(self):
        service = MagicMock()
        service.users().history().list().execute.return_value = {}
        client = _make_client(service)

        result = list(client.list_history(start_history_id=100))

        assert result == []

    def test_raises_history_expired_on_404(self):
        service = MagicMock()
        service.users().history().list().execute.side_effect = _http_error(404)
        client = _make_client(service)

        with pytest.raises(HistoryExpiredError):
            list(client.list_history(start_history_id=999999))

    def test_propagates_non_404_http_errors(self):
        service = MagicMock()
        service.users().history().list().execute.side_effect = _http_error(500)
        client = _make_client(service)

        with pytest.raises(HttpError):
            list(client.list_history(start_history_id=100))

    def test_passes_start_history_id_as_string(self):
        service = MagicMock()
        service.users().history().list().execute.return_value = {"history": []}
        client = _make_client(service)

        list(client.list_history(start_history_id=12345))

        list_calls = service.users().history().list.call_args_list
        # find the call that has startHistoryId — the fluent chain produces
        # some empty-args calls along the way
        target = next(
            c for c in list_calls if "startHistoryId" in c.kwargs
        )
        assert target.kwargs["startHistoryId"] == "12345"
        assert target.kwargs["historyTypes"] == ["messageAdded"]

    def test_custom_history_types_forwarded(self):
        service = MagicMock()
        service.users().history().list().execute.return_value = {"history": []}
        client = _make_client(service)

        list(
            client.list_history(
                start_history_id=1,
                history_types=("messageAdded", "labelAdded"),
            )
        )

        list_calls = service.users().history().list.call_args_list
        target = next(c for c in list_calls if "historyTypes" in c.kwargs)
        assert target.kwargs["historyTypes"] == [
            "messageAdded",
            "labelAdded",
        ]

    def test_is_lazy_generator(self):
        """Generator should not call the API until iterated."""
        service = MagicMock()
        client = _make_client(service)

        gen = client.list_history(start_history_id=1)

        # Constructing the generator should NOT have triggered .list() with
        # startHistoryId (only the fluent chain warm-up MagicMock calls).
        list_calls = service.users().history().list.call_args_list
        assert not any("startHistoryId" in c.kwargs for c in list_calls)
