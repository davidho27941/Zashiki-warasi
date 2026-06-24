"""Tests for the pure parsing helpers in GmailClient.

These do not hit the network; we exercise the private methods directly
on synthetic Gmail API payloads.
"""

from __future__ import annotations

import base64
from datetime import timezone

import pytest

from zashiki_warasi.gmail.client import GmailClient


def _b64url(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


# --- _parse_headers ---


class TestParseHeaders:
    def test_case_insensitive_lowercase_keys(self):
        headers = [
            {"name": "From", "value": "a@x"},
            {"name": "SUBJECT", "value": "hi"},
        ]
        out = GmailClient._parse_headers(headers)
        assert out == {"from": "a@x", "subject": "hi"}

    def test_duplicate_header_values_joined(self):
        headers = [
            {"name": "Received", "value": "by host1"},
            {"name": "Received", "value": "by host2"},
        ]
        out = GmailClient._parse_headers(headers)
        assert out["received"] == "by host1, by host2"

    def test_missing_value_defaults_to_empty(self):
        out = GmailClient._parse_headers([{"name": "X-Empty"}])
        assert out == {"x-empty": ""}

    def test_empty_list(self):
        assert GmailClient._parse_headers([]) == {}


# --- _walk_parts ---


class TestWalkParts:
    def test_flat_payload_yields_self(self):
        payload = {"mimeType": "text/plain", "body": {"data": "abc"}}
        assert list(GmailClient._walk_parts(payload)) == [payload]

    def test_walks_nested_tree_depth_first(self):
        leaf_a = {"mimeType": "text/plain"}
        leaf_b = {"mimeType": "text/html"}
        leaf_c = {"mimeType": "application/pdf"}
        payload = {
            "mimeType": "multipart/mixed",
            "parts": [
                {
                    "mimeType": "multipart/alternative",
                    "parts": [leaf_a, leaf_b],
                },
                leaf_c,
            ],
        }
        assert list(GmailClient._walk_parts(payload)) == [
            leaf_a,
            leaf_b,
            leaf_c,
        ]


# --- _extract_bodies ---


class TestExtractBodies:
    def test_plain_only(self):
        leaves = [
            {"mimeType": "text/plain", "body": {"data": _b64url("hello")}}
        ]
        plain, html = GmailClient._extract_bodies(leaves)
        assert plain == "hello"
        assert html is None

    def test_plain_and_html(self):
        leaves = [
            {"mimeType": "text/plain", "body": {"data": _b64url("hi")}},
            {"mimeType": "text/html", "body": {"data": _b64url("<p>hi</p>")}},
        ]
        plain, html = GmailClient._extract_bodies(leaves)
        assert plain == "hi"
        assert html == "<p>hi</p>"

    def test_first_body_per_mime_wins(self):
        leaves = [
            {"mimeType": "text/plain", "body": {"data": _b64url("first")}},
            {"mimeType": "text/plain", "body": {"data": _b64url("second")}},
        ]
        plain, _ = GmailClient._extract_bodies(leaves)
        assert plain == "first"

    def test_no_text_parts_returns_none(self):
        leaves = [
            {"mimeType": "application/pdf", "body": {"data": _b64url("xx")}}
        ]
        plain, html = GmailClient._extract_bodies(leaves)
        assert plain is None
        assert html is None

    def test_parts_without_data_skipped(self):
        leaves = [
            {"mimeType": "text/plain", "body": {}},
            {"mimeType": "text/plain", "body": {"data": _b64url("real")}},
        ]
        plain, _ = GmailClient._extract_bodies(leaves)
        assert plain == "real"


# --- _decode_body / charset handling ---


class TestDecodeBody:
    def test_utf8_default(self):
        part = {"body": {"data": _b64url("hello")}, "headers": []}
        out = GmailClient._decode_body(part["body"]["data"], part)
        assert out == "hello"

    def test_charset_from_content_type_header(self):
        raw_bytes = "héllo".encode("latin-1")
        data = base64.urlsafe_b64encode(raw_bytes).decode().rstrip("=")
        part = {
            "headers": [
                {
                    "name": "Content-Type",
                    "value": "text/plain; charset=latin-1",
                }
            ],
        }
        assert GmailClient._decode_body(data, part) == "héllo"

    def test_quoted_charset(self):
        part = {
            "headers": [
                {"name": "Content-Type", "value": 'text/plain; charset="UTF-8"'}
            ],
        }
        assert (
            GmailClient._decode_body(_b64url("hi"), part) == "hi"
        )

    def test_decode_errors_replaced_not_raised(self):
        # Invalid UTF-8 byte 0xff at start
        data = base64.urlsafe_b64encode(b"\xffabc").decode().rstrip("=")
        part = {"headers": []}
        out = GmailClient._decode_body(data, part)
        assert "abc" in out  # replacement char + abc


# --- _extract_attachments ---


class TestExtractAttachments:
    def test_attachment_metadata_parsed(self):
        leaves = [
            {
                "mimeType": "application/pdf",
                "filename": "doc.pdf",
                "body": {"attachmentId": "att-1", "size": 9000},
            }
        ]
        out = GmailClient._extract_attachments(leaves)
        assert len(out) == 1
        a = out[0]
        assert a.attachment_id == "att-1"
        assert a.filename == "doc.pdf"
        assert a.mime_type == "application/pdf"
        assert a.size == 9000

    def test_parts_without_filename_or_id_skipped(self):
        leaves = [
            {"mimeType": "text/plain", "body": {}},
            {
                "mimeType": "application/pdf",
                "filename": "",  # filename missing
                "body": {"attachmentId": "x"},
            },
            {
                "mimeType": "application/pdf",
                "filename": "x.pdf",
                "body": {},  # no attachmentId (inline)
            },
        ]
        assert GmailClient._extract_attachments(leaves) == []

    def test_default_mime_type_when_missing(self):
        leaves = [
            {
                "filename": "blob",
                "body": {"attachmentId": "a", "size": 1},
            }
        ]
        out = GmailClient._extract_attachments(leaves)
        assert out[0].mime_type == "application/octet-stream"


# --- address parsing ---


class TestParseAddresses:
    def test_single(self):
        assert GmailClient._parse_addresses("a@x.com") == ["a@x.com"]

    def test_display_name(self):
        assert GmailClient._parse_addresses("Alice <a@x.com>") == ["a@x.com"]

    def test_multiple_comma_separated(self):
        out = GmailClient._parse_addresses("Alice <a@x>, b@x, Carol <c@x>")
        assert out == ["a@x", "b@x", "c@x"]

    def test_empty(self):
        assert GmailClient._parse_addresses("") == []

    def test_first_address(self):
        assert (
            GmailClient._first_address("Alice <a@x>, b@x") == "a@x"
        )

    def test_first_address_empty(self):
        assert GmailClient._first_address("") == ""


# --- _parse_message end-to-end ---


class TestParseMessage:
    @pytest.fixture
    def raw_message(self):
        return {
            "id": "msg-1",
            "threadId": "thread-1",
            "historyId": "12345",
            "internalDate": "1718950000000",
            "labelIds": ["INBOX", "UNREAD"],
            "snippet": "preview text",
            "payload": {
                "mimeType": "multipart/alternative",
                "headers": [
                    {"name": "From", "value": "Alice <alice@example.com>"},
                    {"name": "To", "value": "bob@example.com, carol@example.com"},
                    {"name": "Cc", "value": "dave@example.com"},
                    {"name": "Subject", "value": "Quarterly report"},
                    {"name": "Reply-To", "value": "noreply@example.com"},
                ],
                "parts": [
                    {
                        "mimeType": "text/plain",
                        "body": {"data": _b64url("plain body")},
                    },
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64url("<p>html body</p>")},
                    },
                    {
                        "mimeType": "application/pdf",
                        "filename": "report.pdf",
                        "body": {"attachmentId": "att-1", "size": 12345},
                    },
                ],
            },
        }

    def test_core_fields(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.id == "msg-1"
        assert msg.thread_id == "thread-1"
        assert msg.history_id == 12345
        assert msg.snippet == "preview text"
        assert msg.subject == "Quarterly report"

    def test_addresses(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.from_address == "alice@example.com"
        assert msg.to_addresses == ["bob@example.com", "carol@example.com"]
        assert msg.cc_addresses == ["dave@example.com"]

    def test_bodies_decoded(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.body_plain == "plain body"
        assert msg.body_html == "<p>html body</p>"

    def test_attachments(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert len(msg.attachments) == 1
        assert msg.attachments[0].filename == "report.pdf"
        assert msg.attachments[0].size == 12345

    def test_labels(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.labels == ["INBOX", "UNREAD"]

    def test_received_at_is_utc(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.received_at.tzinfo == timezone.utc

    def test_raw_headers_includes_uncommon(self, raw_message):
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw_message)
        assert msg.raw_headers["reply-to"] == "noreply@example.com"
        assert msg.raw_headers["subject"] == "Quarterly report"

    def test_message_without_parts_uses_payload_body(self):
        raw = {
            "id": "msg-2",
            "threadId": "t-2",
            "historyId": "100",
            "internalDate": "1718950000000",
            "labelIds": [],
            "snippet": "",
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": "x@y"},
                    {"name": "Subject", "value": "flat"},
                ],
                "body": {"data": _b64url("just text")},
            },
        }
        client = GmailClient.__new__(GmailClient)
        msg = client._parse_message(raw)
        assert msg.body_plain == "just text"
        assert msg.body_html is None
        assert msg.attachments == []
