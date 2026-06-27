"""PDF text extraction helpers for the expense vertical.

Text-based PDFs (modern e-receipts from Amazon, Rakuten, etc.) extract
cleanly via pdfplumber. Image-only / scanned / encrypted PDFs return an
empty string so the caller can route to a manual-review fallback.
"""

from __future__ import annotations

import io
import logging

import pdfplumber

from zashiki_warasi.core.schemas import EmailMessage
from zashiki_warasi.gmail.client import GmailClient

logger = logging.getLogger(__name__)


def pdf_extract_text(pdf_bytes: bytes) -> str:
    """Best-effort text extraction. Empty string means image-only,
    encrypted, or corrupt — caller must NOT treat that as 'no expense
    info' (information was present but unreadable)."""
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            parts = [page.extract_text() or "" for page in pdf.pages]
    except Exception as exc:
        logger.warning(f"PDF extract failed: {exc}")
        return ""
    return "\n".join(parts).strip()


def collect_text(
    email: EmailMessage, client: GmailClient
) -> tuple[str, list[str]]:
    """Combine email body + extractable PDF attachment text.

    Returns:
      (combined_text, unreadable_pdf_filenames)
      A non-empty unreadable list signals that the email DID have a PDF
      we couldn't read — the caller should route to needs_review rather
      than feed the LLM a thin context that invites hallucination.
    """
    chunks: list[str] = []
    body = email.body_plain or email.snippet or ""
    if body.strip():
        chunks.append(body)

    unreadable: list[str] = []
    for att in email.attachments:
        if att.mime_type != "application/pdf":
            continue
        pdf_bytes = client.get_attachment(email.id, att.attachment_id)
        text = pdf_extract_text(pdf_bytes)
        if text:
            chunks.append(f"=== 附件: {att.filename} ===\n{text}")
        else:
            unreadable.append(att.filename)

    return "\n\n".join(chunks), unreadable
