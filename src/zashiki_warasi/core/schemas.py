"""Shared data models passed between Gmail layer and agents."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class AttachmentMeta(BaseModel):
    """Metadata for an attachment; bytes fetched on demand via GmailClient."""

    model_config = ConfigDict(frozen=True)

    attachment_id: str
    filename: str
    mime_type: str
    size: int


class EmailMessage(BaseModel):
    """Parsed Gmail message ready for agent consumption."""

    model_config = ConfigDict(frozen=True)

    id: str
    thread_id: str
    history_id: int
    from_address: str
    to_addresses: list[str] = Field(default_factory=list)
    cc_addresses: list[str] = Field(default_factory=list)
    subject: str = ""
    snippet: str = ""
    body_plain: str | None = None
    body_html: str | None = None
    received_at: datetime
    labels: list[str] = Field(default_factory=list)
    attachments: list[AttachmentMeta] = Field(default_factory=list)
    raw_headers: dict[str, str] = Field(default_factory=dict)


class ProfileInfo(BaseModel):
    """Gmail account profile used to baseline history polling."""

    model_config = ConfigDict(frozen=True)

    email: str
    history_id: int


class EmailAnalysis(BaseModel):
    """LLM-produced summary and classification of a single email.

    Used as the structured output schema for the analyze node; the same
    fields are persisted to the `email_analyses` table.
    """

    category: Literal[
        "work",
        "personal",
        "promotional",
        "newsletter",
        "transactional",
        "other",
    ] = Field(description="High-level intent / kind of message.")
    importance: Literal["high", "medium", "low"] = Field(
        description="How urgently the user should act on this email."
    )
    summary: str = Field(
        description="One to two sentences capturing the key point.",
    )
