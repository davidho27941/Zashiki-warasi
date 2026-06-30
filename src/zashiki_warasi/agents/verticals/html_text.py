"""HTML → plain-text conversion for the expense / analyze pipelines.

Used as a fallback when an email has no `body_plain` (HTML-only
notifications, modern e-receipts). Output is fed straight into the
LLM prompt, so the conversion is tuned for low noise:

- inline images dropped (no `![alt](src)` markdown lines)
- hyperlinks dropped (no `[text](url)` clutter; the visible text is
  kept on its own)
- emphasis / bold stripped (the LLM doesn't need * or _ markers)
- long line wrapping disabled (preserves the original visual line
  breaks rather than re-flowing at 78 chars)
"""

from __future__ import annotations

import logging

import html2text

logger = logging.getLogger(__name__)


def _build_converter() -> html2text.HTML2Text:
    h = html2text.HTML2Text()
    h.ignore_links = True
    h.ignore_images = True
    h.ignore_emphasis = True
    h.body_width = 0  # don't re-wrap; keep source line structure
    h.unicode_snob = True  # keep CJK / curly quotes as-is, no entities
    h.skip_internal_links = True
    return h


def html_to_text(html: str | None) -> str:
    """Convert HTML to LLM-friendly plain text.

    Returns an empty string for None / empty / whitespace-only input;
    catches malformed-HTML exceptions defensively so a single weird
    email cannot crash the polling loop.
    """
    if not html or not html.strip():
        return ""
    try:
        converter = _build_converter()
        return converter.handle(html).strip()
    except Exception as exc:
        logger.warning(
            f"html_to_text: conversion failed ({type(exc).__name__}: {exc}); "
            "returning empty string"
        )
        return ""
