"""PDF text extraction helpers: pdf_extract_text + collect_text."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from zashiki_warasi.agents.verticals.pdf import collect_text, pdf_extract_text
from zashiki_warasi.core.schemas import AttachmentMeta, EmailMessage


def _email(
    *, body: str | None = "body text", attachments=None
) -> EmailMessage:
    return EmailMessage(
        id="m1",
        thread_id="t",
        history_id=1,
        from_address="x@y.com",
        subject="subj",
        body_plain=body,
        received_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
        attachments=attachments or [],
    )


def _pdf_attachment(filename: str = "r.pdf") -> AttachmentMeta:
    return AttachmentMeta(
        attachment_id="att1",
        filename=filename,
        mime_type="application/pdf",
        size=1000,
    )


def _mock_pdfplumber_open(pages_text: list[str | None]):
    """Return a context-manager mock for pdfplumber.open() yielding the
    given pages' extract_text values."""
    pages = []
    for text in pages_text:
        page = MagicMock()
        page.extract_text.return_value = text
        pages.append(page)
    pdf = MagicMock()
    pdf.pages = pages
    pdf.__enter__ = MagicMock(return_value=pdf)
    pdf.__exit__ = MagicMock(return_value=None)
    return pdf


# --- pdf_extract_text ---


class TestPdfExtractText:
    def test_concatenates_pages_with_newlines(self):
        pdf = _mock_pdfplumber_open(["page one", "page two"])
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            return_value=pdf,
        ):
            assert pdf_extract_text(b"fake") == "page one\npage two"

    def test_single_page(self):
        pdf = _mock_pdfplumber_open(["only page"])
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            return_value=pdf,
        ):
            assert pdf_extract_text(b"fake") == "only page"

    def test_page_returning_none_treated_as_empty(self):
        # None page -> "", joined with "\npage two" then strip()ed.
        pdf = _mock_pdfplumber_open([None, "page two"])
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            return_value=pdf,
        ):
            assert pdf_extract_text(b"fake") == "page two"

    def test_all_pages_none_returns_empty_string(self):
        """Image-only PDF surrogate: pdfplumber opens fine but no text."""
        pdf = _mock_pdfplumber_open([None, None])
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            return_value=pdf,
        ):
            assert pdf_extract_text(b"fake") == ""

    def test_open_failure_returns_empty_not_raises(self):
        """Corrupt / encrypted PDF surrogate."""
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            side_effect=Exception("corrupt"),
        ):
            assert pdf_extract_text(b"fake") == ""

    def test_whitespace_stripped_overall(self):
        pdf = _mock_pdfplumber_open(["  hello  "])
        with patch(
            "zashiki_warasi.agents.verticals.pdf.pdfplumber.open",
            return_value=pdf,
        ):
            assert pdf_extract_text(b"fake") == "hello"


# --- collect_text ---


class TestCollectText:
    def test_body_only_no_attachments(self):
        email = _email(body="just body")
        client = MagicMock()
        text, unreadable = collect_text(email, client)
        assert text == "just body"
        assert unreadable == []
        client.get_attachment.assert_not_called()

    def test_falls_back_to_snippet_when_body_plain_missing(self):
        email = EmailMessage(
            id="m",
            thread_id="t",
            history_id=1,
            from_address="x@y.com",
            subject="s",
            snippet="snippet text",
            body_plain=None,
            received_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
        )
        text, _ = collect_text(email, MagicMock())
        assert text == "snippet text"

    def test_skips_non_pdf_attachments(self):
        jpeg = AttachmentMeta(
            attachment_id="a",
            filename="img.jpg",
            mime_type="image/jpeg",
            size=100,
        )
        email = _email(attachments=[jpeg])
        client = MagicMock()
        text, unreadable = collect_text(email, client)
        assert text == "body text"
        assert unreadable == []
        client.get_attachment.assert_not_called()

    def test_combines_body_and_pdf_text(self, monkeypatch):
        email = _email(attachments=[_pdf_attachment("receipt.pdf")])
        client = MagicMock()
        client.get_attachment.return_value = b"pdf bytes"
        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            lambda _b: "EXTRACTED FROM PDF",
        )

        text, unreadable = collect_text(email, client)

        assert "body text" in text
        assert "EXTRACTED FROM PDF" in text
        assert "receipt.pdf" in text  # filename header in chunk
        assert unreadable == []

    def test_records_unreadable_pdfs(self, monkeypatch):
        email = _email(attachments=[_pdf_attachment("scan.pdf")])
        client = MagicMock()
        client.get_attachment.return_value = b"image-only"
        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            lambda _b: "",
        )

        text, unreadable = collect_text(email, client)

        # Body still included; only PDF was unreadable.
        assert "body text" in text
        assert "scan.pdf" not in text  # filename only appears with extracted text
        assert unreadable == ["scan.pdf"]

    def test_empty_body_with_only_unreadable_pdfs(self, monkeypatch):
        email = EmailMessage(
            id="m",
            thread_id="t",
            history_id=1,
            from_address="x@y.com",
            subject="s",
            body_plain=None,
            snippet="",
            received_at=datetime(2026, 6, 27, tzinfo=timezone.utc),
            attachments=[_pdf_attachment("scan.pdf")],
        )
        client = MagicMock()
        client.get_attachment.return_value = b"bytes"
        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            lambda _b: "",
        )

        text, unreadable = collect_text(email, client)

        # Caller will see (empty, [scan.pdf]) and route to needs_review.
        assert text == ""
        assert unreadable == ["scan.pdf"]

    def test_mixed_readable_and_unreadable_pdfs(self, monkeypatch):
        readable = _pdf_attachment("good.pdf")
        unreadable_att = _pdf_attachment("bad.pdf")
        email = _email(attachments=[readable, unreadable_att])
        client = MagicMock()
        client.get_attachment.return_value = b"bytes"

        call_count = {"n": 0}

        def fake_extract(_b):
            call_count["n"] += 1
            return "GOOD CONTENT" if call_count["n"] == 1 else ""

        monkeypatch.setattr(
            "zashiki_warasi.agents.verticals.pdf.pdf_extract_text",
            fake_extract,
        )

        text, unreadable = collect_text(email, client)

        assert "GOOD CONTENT" in text
        assert unreadable == ["bad.pdf"]
