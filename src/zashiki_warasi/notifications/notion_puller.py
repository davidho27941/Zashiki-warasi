"""Notion → Postgres reverse sync.

`NotionExpensePuller` polls the configured Notion expense database for
pages whose `last_edited_time` is newer than our cursor and writes any
field changes back to the local `ExpenseRecord`. Pages without our
auto-generated marker in the 備註 column are ignored so user-created
rows in the same database don't get touched.

Conflict policy: Notion wins (latest-write-wins). The assumption is
that users edit Notion when the LLM extracted something wrong, so
their edits should override the original extraction.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from notion_client import Client
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from zashiki_warasi.core.config import NotionSettings
from zashiki_warasi.core.models import ExpenseRecord, NotionSyncState
from zashiki_warasi.notifications.notion import NotionExpenseRecorder

logger = logging.getLogger(__name__)


# Fields the puller will copy from Notion back to ExpenseRecord. These
# correspond to the columns a human is plausibly correcting after the
# LLM's first pass; immutable / agent-only fields (message_id, raw
# extraction, transaction_id, notes marker) are deliberately excluded.
_SYNCABLE_FIELDS: tuple[str, ...] = (
    "title",
    "vendor",
    "amount",
    "currency",
    "transacted_at",
    "category",
    "payment_method",
)

# Reverse the recorder's currency label map so "日幣" → "JPY" round-trip.
_LABEL_TO_CURRENCY = {
    label: code
    for code, label in NotionExpenseRecorder._CURRENCY_LABELS.items()
}


@dataclass(frozen=True)
class SyncStats:
    fetched: int
    updated: int
    skipped: int

    def __str__(self) -> str:
        return (
            f"fetched={self.fetched} updated={self.updated} "
            f"skipped={self.skipped}"
        )


class NotionExpensePuller:
    """Pulls recent edits from the Notion expense DB into Postgres."""

    PAGE_SIZE = 100

    def __init__(
        self,
        settings: NotionSettings,
        session_factory: sessionmaker[Session],
        client: Client | None = None,
    ) -> None:
        if not settings.token:
            raise ValueError("NOTION_TOKEN is not set.")
        if not settings.expense_database_id:
            raise ValueError("NOTION_EXPENSE_DATABASE_ID is not set.")
        self._settings = settings
        self._session_factory = session_factory
        self._client = client or Client(
            auth=settings.token,
            timeout_ms=int(settings.timeout_seconds * 1000),
        )
        # Resolved on first call to `_fetch_recent_pages` and cached
        # for the puller's lifetime — data_source_id doesn't change.
        # Notion's 2025 API split databases into 1+ data sources;
        # query was moved from `databases.query` (removed in
        # notion-client 3.x) to `data_sources.query`.
        self._data_source_id: str | None = None

    def sync_once(self) -> SyncStats:
        """Run one full reconcile pass; return counts."""
        cursor = self._read_cursor()
        pages = self._fetch_recent_pages(cursor)

        updated = 0
        skipped = 0
        max_edited = cursor

        for page in pages:
            edited_at = _parse_iso(page.get("last_edited_time"))
            if edited_at is not None and (
                max_edited is None or edited_at > max_edited
            ):
                max_edited = edited_at
            if self._apply_page(page, edited_at):
                updated += 1
            else:
                skipped += 1

        # Only advance the cursor if we actually saw a newer edit time
        # — guards against a clock jump or empty result rewinding state.
        if max_edited is not None and max_edited != cursor:
            self._write_cursor(max_edited)

        return SyncStats(fetched=len(pages), updated=updated, skipped=skipped)

    def _resolve_data_source_id(self) -> str:
        """Look up the data_source_id for the configured database.

        Notion's 2025 model: each database has one or more data
        sources; `data_sources.query` operates on the data source,
        not the database. For our use case (a single expense DB) the
        first data source is the only one and we cache it for the
        process lifetime.
        """
        if self._data_source_id is not None:
            return self._data_source_id
        database = self._client.databases.retrieve(
            database_id=self._settings.expense_database_id
        )
        data_sources = database.get("data_sources") or []
        if not data_sources:
            raise RuntimeError(
                f"Notion database {self._settings.expense_database_id} "
                "has no data_sources — cannot query."
            )
        self._data_source_id = data_sources[0]["id"]
        return self._data_source_id

    def _fetch_recent_pages(
        self, cursor: datetime | None
    ) -> list[dict[str, Any]]:
        """Query Notion for pages edited after `cursor` and marked as
        auto-generated. Paginates through `has_more`."""
        marker = NotionExpenseRecorder.AUTO_GENERATED_NOTE
        notes_prop = NotionExpenseRecorder.PROP_NOTES
        data_source_id = self._resolve_data_source_id()

        and_filters: list[dict[str, Any]] = [
            {
                "property": notes_prop,
                "rich_text": {"contains": marker},
            },
        ]
        if cursor is not None:
            and_filters.append(
                {
                    "timestamp": "last_edited_time",
                    "last_edited_time": {"after": cursor.isoformat()},
                }
            )

        results: list[dict[str, Any]] = []
        start_cursor: str | None = None
        while True:
            query: dict[str, Any] = {
                "data_source_id": data_source_id,
                "filter": {"and": and_filters},
                "sorts": [
                    {
                        "timestamp": "last_edited_time",
                        "direction": "ascending",
                    }
                ],
                "page_size": self.PAGE_SIZE,
            }
            if start_cursor is not None:
                query["start_cursor"] = start_cursor
            response = self._client.data_sources.query(**query)
            results.extend(response.get("results", []))
            if not response.get("has_more"):
                break
            start_cursor = response.get("next_cursor")
            if not start_cursor:
                break
        return results

    def _apply_page(
        self, page: dict[str, Any], edited_at: datetime | None
    ) -> bool:
        page_id = page.get("id")
        if not page_id:
            logger.warning(f"Notion page missing id: {page!r}")
            return False

        updates = _extract_updates(page.get("properties", {}))

        with self._session_factory() as session:
            expense = session.scalar(
                select(ExpenseRecord).where(
                    ExpenseRecord.notion_page_id == page_id
                )
            )
            if expense is None:
                logger.warning(
                    f"Notion page {page_id} has no matching local "
                    "ExpenseRecord; skipping"
                )
                return False

            diff = {
                field: (getattr(expense, field), new)
                for field, new in updates.items()
                if field in _SYNCABLE_FIELDS
                and getattr(expense, field) != new
            }
            if not diff:
                # Page was edited but nothing we care about changed
                # (e.g. user toggled a non-synced property).
                return False

            for field, (_old, new) in diff.items():
                setattr(expense, field, new)
            expense.notion_synced_at = edited_at or _utcnow()
            session.commit()

            human_diff = {
                f: f"{old!r}→{new!r}" for f, (old, new) in diff.items()
            }
            logger.info(
                f"Synced Notion page {page_id} → expense {expense.id}: "
                f"{human_diff}"
            )
            return True

    def _read_cursor(self) -> datetime | None:
        with self._session_factory() as session:
            state = session.get(
                NotionSyncState, self._settings.expense_database_id
            )
            return state.last_synced_at if state else None

    def _write_cursor(self, value: datetime) -> None:
        with self._session_factory() as session:
            state = session.get(
                NotionSyncState, self._settings.expense_database_id
            )
            if state is None:
                state = NotionSyncState(
                    database_id=self._settings.expense_database_id,
                    last_synced_at=value,
                )
                session.add(state)
            else:
                state.last_synced_at = value
            session.commit()


def _extract_updates(properties: dict[str, Any]) -> dict[str, Any]:
    """Translate a Notion page's properties dict into model fields.

    Skips any field whose property is missing or empty so we don't
    overwrite a populated DB value with None. Currency labels are
    reverse-mapped via `_LABEL_TO_CURRENCY`."""
    out: dict[str, Any] = {}

    title = _read_title(properties.get(NotionExpenseRecorder.PROP_TITLE))
    if title is not None:
        out["title"] = title

    vendor = _read_rich_text(
        properties.get(NotionExpenseRecorder.PROP_VENDOR)
    )
    if vendor is not None:
        out["vendor"] = vendor

    amount = _read_number(
        properties.get(NotionExpenseRecorder.PROP_AMOUNT)
    )
    if amount is not None:
        out["amount"] = Decimal(str(amount))

    currency_label = _read_select(
        properties.get(NotionExpenseRecorder.PROP_CURRENCY)
    )
    if currency_label is not None:
        out["currency"] = _LABEL_TO_CURRENCY.get(
            currency_label, currency_label
        )

    transacted = _read_date(
        properties.get(NotionExpenseRecorder.PROP_TRANSACTED_AT)
    )
    if transacted is not None:
        out["transacted_at"] = transacted

    category = _read_select(
        properties.get(NotionExpenseRecorder.PROP_CATEGORY)
    )
    if category is not None:
        out["category"] = category

    payment_method = _read_select(
        properties.get(NotionExpenseRecorder.PROP_PAYMENT_METHOD)
    )
    if payment_method is not None:
        out["payment_method"] = payment_method

    return out


def _read_title(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("title") or []
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def _read_rich_text(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    parts = prop.get("rich_text") or []
    text = "".join(p.get("plain_text", "") for p in parts).strip()
    return text or None


def _read_number(prop: dict[str, Any] | None) -> float | None:
    if not prop:
        return None
    return prop.get("number")


def _read_select(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    select_obj = prop.get("select")
    if not select_obj:
        return None
    return select_obj.get("name")


def _read_date(prop: dict[str, Any] | None) -> datetime | None:
    if not prop:
        return None
    date_obj = prop.get("date")
    if not date_obj:
        return None
    parsed = _parse_iso(date_obj.get("start"))
    if parsed is not None and parsed.tzinfo is None:
        # Notion date-only values ("2026-06-21") parse as naive — pin
        # to UTC midnight so they store cleanly in the tzaware column.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _parse_iso(value: str | None) -> datetime | None:
    """Parse a Notion-style ISO 8601 string ('Z' suffix or offset)."""
    if not value:
        return None
    # Notion uses Z; datetime.fromisoformat handles +00:00 cleanly in 3.13.
    normalised = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalised)
    except ValueError:
        logger.warning(f"Unparseable Notion timestamp: {value!r}")
        return None


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)
