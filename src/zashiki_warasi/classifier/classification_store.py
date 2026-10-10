"""SCD type 2 writer for `email_classifications`.

Every classification write goes through `record`, which keeps the
history invariants the schema enforces on Postgres (one current row,
no overlaps) and the policy the schema cannot express:

- a new version opens only when `(category, taxonomy_version, source)`
  changes — replays and rescans are no-ops;
- a human row is never closed by a machine source (llm / laya);
- closing the old row and opening the new one share one timestamp,
  inside the caller's transaction.

`record` never commits: the caller owns the transaction, so the LLM
path can write `email_analyses` and the first history row atomically.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from zashiki_warasi.core.models import EmailClassification
from zashiki_warasi.core.schemas import CATEGORIES_BY_VERSION

logger = logging.getLogger(__name__)

Source = Literal["llm", "laya", "human"]
RecordedBy = Literal["app", "migration", "import"]
Outcome = Literal["inserted", "versioned", "unchanged", "human_kept"]

_MACHINE_SOURCES = frozenset({"llm", "laya"})


def _current(session: Session, message_id: str) -> EmailClassification | None:
    return session.scalar(
        select(EmailClassification)
        .where(
            EmailClassification.message_id == message_id,
            EmailClassification.valid_to.is_(None),
        )
        .with_for_update()
    )


def _as_aware(value: datetime) -> datetime:
    # SQLite hands back naive datetimes; Postgres returns aware ones.
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def record(
    session: Session,
    *,
    message_id: str,
    category: str,
    taxonomy_version: str,
    source: Source,
    recorded_by: RecordedBy,
    at: datetime | None = None,
) -> Outcome:
    """Record one classification judgement for `message_id`.

    `at` is the processing time the judgement takes effect (defaults
    to now, UTC). Raises ValueError for a category outside its
    taxonomy version or an `at` not after the current row's start.
    """
    if category not in CATEGORIES_BY_VERSION.get(taxonomy_version, ()):
        raise ValueError(
            f"category {category!r} is not in taxonomy {taxonomy_version!r}"
        )
    at = _as_aware(at) if at is not None else datetime.now(timezone.utc)

    for attempt in (1, 2):
        current = _current(session, message_id)
        if current is not None:
            if (
                current.category == category
                and current.taxonomy_version == taxonomy_version
                and current.source == source
            ):
                return "unchanged"
            if current.source == "human" and source in _MACHINE_SOURCES:
                return "human_kept"
            if at <= _as_aware(current.valid_from):
                raise ValueError(
                    f"classification for {message_id} at {at.isoformat()} "
                    f"does not follow the current row "
                    f"({_as_aware(current.valid_from).isoformat()})"
                )
        try:
            with session.begin_nested():
                if current is not None:
                    current.valid_to = at
                    session.flush()
                session.add(EmailClassification(
                    message_id=message_id,
                    category=category,
                    taxonomy_version=taxonomy_version,
                    source=source,
                    recorded_by=recorded_by,
                    valid_from=at,
                ))
                session.flush()
        except IntegrityError:
            # A concurrent writer opened a current row between our read
            # and insert. Re-read once and re-apply the rules.
            if attempt == 2:
                raise
            logger.info(
                f"classification: concurrent write for {message_id}; retrying"
            )
            session.expire_all()
            continue
        return "inserted" if current is None else "versioned"
    raise AssertionError("unreachable")
