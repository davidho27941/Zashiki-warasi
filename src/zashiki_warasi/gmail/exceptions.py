"""Domain exceptions raised by the Gmail layer."""

from __future__ import annotations


class GmailError(Exception):
    """Base class for all Gmail-layer errors."""


class HistoryExpiredError(GmailError):
    """startHistoryId is older than Gmail's retention window (~7 days).

    The caller must re-baseline by reading the current profile historyId
    and discarding/processing the backlog separately.
    """


class MessageNotFoundError(GmailError):
    """Message was deleted or no longer accessible (HTTP 404)."""
