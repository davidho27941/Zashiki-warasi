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


class CredentialRefreshError(GmailError):
    """OAuth refresh token is expired, revoked, or otherwise invalid.

    Unrecoverable inside the running process — the user must re-run the
    InstalledAppFlow (e.g. via `zashiki-warasi reauth`) to obtain a new
    refresh token. Retrying the same refresh will keep failing.
    """
