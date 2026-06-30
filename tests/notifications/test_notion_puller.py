"""NotionExpensePuller: query filter, field mapping, cursor advance."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.core.config import NotionSettings
from zashiki_warasi.core.models import ExpenseRecord, NotionSyncState
from zashiki_warasi.notifications.notion import NotionExpenseRecorder
from zashiki_warasi.notifications.notion_puller import (
    NotionExpensePuller,
    SyncStats,
    _extract_updates,
    _parse_iso,
)


DB_ID = "db-uuid-abc"


def _settings(**overrides) -> NotionSettings:
    base = dict(
        token="secret_xxx",
        expense_database_id=DB_ID,
        timeout_seconds=5.0,
        sync_interval_seconds=300,
    )
    base.update(overrides)
    return NotionSettings(**base)


def _page(
    *,
    page_id: str = "page-1",
    edited: str = "2026-06-30T10:00:00.000Z",
    title: str | None = "Coffee",
    vendor: str | None = "Starbucks",
    amount: float | None = 1198.0,
    currency_label: str | None = "日幣",
    transacted: str | None = "2026-06-21",
    category: str | None = "Food",
    payment: str | None = "Credit",
) -> dict:
    """Build a Notion page object shaped like the API response."""
    props: dict = {
        NotionExpenseRecorder.PROP_TITLE: {
            "title": (
                [{"plain_text": title}] if title is not None else []
            )
        },
        NotionExpenseRecorder.PROP_VENDOR: {
            "rich_text": (
                [{"plain_text": vendor}] if vendor is not None else []
            )
        },
        NotionExpenseRecorder.PROP_AMOUNT: {"number": amount},
        NotionExpenseRecorder.PROP_CURRENCY: {
            "select": (
                {"name": currency_label}
                if currency_label is not None
                else None
            )
        },
        NotionExpenseRecorder.PROP_TRANSACTED_AT: {
            "date": (
                {"start": transacted} if transacted is not None else None
            )
        },
        NotionExpenseRecorder.PROP_CATEGORY: {
            "select": (
                {"name": category} if category is not None else None
            )
        },
        NotionExpenseRecorder.PROP_PAYMENT_METHOD: {
            "select": (
                {"name": payment} if payment is not None else None
            )
        },
    }
    return {
        "id": page_id,
        "last_edited_time": edited,
        "properties": props,
    }


def _expense(**overrides) -> ExpenseRecord:
    base = dict(
        id=uuid.uuid4(),
        message_id="m-1",
        title="Old title",
        amount=Decimal("999"),
        currency="USD",
        transacted_at=datetime(2026, 6, 1, tzinfo=timezone.utc),
        vendor="Old vendor",
        location=None,
        category="Old cat",
        transaction_id=None,
        payment_method="Old pay",
        raw_extraction={},
        notion_page_id="page-1",
        notion_sync_error=None,
        notion_synced_at=None,
    )
    base.update(overrides)
    return ExpenseRecord(**base)


class _FakeSession:
    """Stand-in for SQLAlchemy Session used by the puller.

    The puller uses three operations only:
      - `scalar(select(ExpenseRecord).where(...))`
      - `get(NotionSyncState, key)`
      - `add(state)` / `commit()`
    """

    def __init__(
        self,
        expenses_by_page: dict[str, ExpenseRecord] | None = None,
        cursor: NotionSyncState | None = None,
    ) -> None:
        self.expenses_by_page = expenses_by_page or {}
        self.cursor = cursor
        self.committed = False
        self.added: list = []
        self._last_select_page_id: str | None = None

    def scalar(self, stmt):
        # Crude but sufficient: pull the literal page_id off the
        # compiled where clause via the stmt's parameters.
        compiled = stmt.compile(compile_kwargs={"literal_binds": True})
        sql = str(compiled)
        for page_id in self.expenses_by_page:
            if f"'{page_id}'" in sql:
                return self.expenses_by_page[page_id]
        return None

    def get(self, model, key):
        if model is NotionSyncState and self.cursor is not None:
            if self.cursor.database_id == key:
                return self.cursor
        return None

    def add(self, obj) -> None:
        self.added.append(obj)
        if isinstance(obj, NotionSyncState):
            self.cursor = obj

    def commit(self) -> None:
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


class _FakeSessionFactory:
    """Yields the same _FakeSession every call so test assertions can
    inspect commit state across the puller's multiple `with` blocks."""

    def __init__(self, session: _FakeSession) -> None:
        self.session = session

    def __call__(self):
        return self.session


def _puller(
    *,
    session: _FakeSession,
    query_pages: list[dict] | list[list[dict]] | None = None,
) -> tuple[NotionExpensePuller, MagicMock]:
    client = MagicMock()
    if query_pages is None:
        query_pages = []
    # Allow a list-of-lists to simulate pagination (has_more=True for
    # all but the last batch). Empty input → one empty batch so the
    # puller still gets a valid (empty) response object.
    if not query_pages:
        batches: list[list[dict]] = [[]]
    elif isinstance(query_pages[0], dict):
        batches = [query_pages]
    else:
        batches = query_pages
    responses = []
    for i, batch in enumerate(batches):
        responses.append(
            {
                "results": batch,
                "has_more": i < len(batches) - 1,
                "next_cursor": f"cur-{i + 1}" if i < len(batches) - 1 else None,
            }
        )
    client.databases.query.side_effect = responses

    puller = NotionExpensePuller(
        _settings(), _FakeSessionFactory(session), client=client
    )
    return puller, client


# --- _parse_iso ---


class TestParseIso:
    def test_parses_z_suffix(self):
        result = _parse_iso("2026-06-30T10:00:00.000Z")
        assert result == datetime(
            2026, 6, 30, 10, 0, 0, tzinfo=timezone.utc
        )

    def test_parses_offset(self):
        result = _parse_iso("2026-06-30T10:00:00+09:00")
        assert result is not None
        assert result.utcoffset().total_seconds() == 9 * 3600

    def test_returns_none_on_garbage(self):
        assert _parse_iso("not a date") is None

    def test_returns_none_on_empty(self):
        assert _parse_iso(None) is None
        assert _parse_iso("") is None


# --- _extract_updates (pure mapping) ---


class TestExtractUpdates:
    def test_maps_all_fields(self):
        page = _page()
        updates = _extract_updates(page["properties"])
        assert updates["title"] == "Coffee"
        assert updates["vendor"] == "Starbucks"
        assert updates["amount"] == Decimal("1198.0")
        assert updates["currency"] == "JPY"  # 日幣 → JPY reverse mapping
        assert updates["category"] == "Food"
        assert updates["payment_method"] == "Credit"
        assert updates["transacted_at"] == datetime(
            2026, 6, 21, tzinfo=timezone.utc
        )

    def test_unknown_currency_label_passes_through(self):
        page = _page(currency_label="Euro")
        updates = _extract_updates(page["properties"])
        # Reverse map only knows JPY/TWD/USD — unknown labels stay raw
        # so a debugging human can see what the user typed in Notion.
        assert updates["currency"] == "Euro"

    def test_empty_properties_yield_no_updates(self):
        page = _page(
            title=None,
            vendor=None,
            amount=None,
            currency_label=None,
            transacted=None,
            category=None,
            payment=None,
        )
        updates = _extract_updates(page["properties"])
        assert updates == {}

    def test_amount_becomes_decimal(self):
        page = _page(amount=42.5)
        updates = _extract_updates(page["properties"])
        assert isinstance(updates["amount"], Decimal)


# --- query filter shape ---


class TestQueryFilter:
    def test_first_run_omits_last_edited_filter(self):
        session = _FakeSession()
        puller, client = _puller(session=session, query_pages=[])
        puller.sync_once()

        kwargs = client.databases.query.call_args.kwargs
        and_filters = kwargs["filter"]["and"]
        # Only the auto-generated marker filter; no time filter.
        assert len(and_filters) == 1
        assert and_filters[0]["property"] == NotionExpenseRecorder.PROP_NOTES
        assert (
            and_filters[0]["rich_text"]["contains"]
            == NotionExpenseRecorder.AUTO_GENERATED_NOTE
        )

    def test_subsequent_run_adds_after_filter(self):
        cursor_dt = datetime(2026, 6, 29, 12, 0, tzinfo=timezone.utc)
        cursor = NotionSyncState(
            database_id=DB_ID, last_synced_at=cursor_dt
        )
        session = _FakeSession(cursor=cursor)
        puller, client = _puller(session=session, query_pages=[])
        puller.sync_once()

        and_filters = client.databases.query.call_args.kwargs["filter"][
            "and"
        ]
        assert len(and_filters) == 2
        time_filter = next(
            f for f in and_filters if "timestamp" in f
        )
        assert time_filter["timestamp"] == "last_edited_time"
        assert (
            time_filter["last_edited_time"]["after"] == cursor_dt.isoformat()
        )

    def test_sorts_ascending_by_last_edited(self):
        session = _FakeSession()
        puller, client = _puller(session=session, query_pages=[])
        puller.sync_once()
        sorts = client.databases.query.call_args.kwargs["sorts"]
        assert sorts == [
            {"timestamp": "last_edited_time", "direction": "ascending"}
        ]


# --- apply / diff behaviour ---


class TestSyncOnce:
    def test_updates_changed_fields_only(self):
        expense = _expense(
            title="Old title", vendor="Old vendor", currency="USD"
        )
        session = _FakeSession(expenses_by_page={"page-1": expense})
        # New page has different title + vendor + currency but same
        # category/payment as the old expense.
        page = _page(
            page_id="page-1",
            title="New title",
            vendor="New vendor",
            currency_label="日幣",  # → JPY
            category="Old cat",
            payment="Old pay",
        )
        puller, _ = _puller(session=session, query_pages=[page])

        stats = puller.sync_once()

        assert stats == SyncStats(fetched=1, updated=1, skipped=0)
        assert expense.title == "New title"
        assert expense.vendor == "New vendor"
        assert expense.currency == "JPY"
        assert expense.notion_synced_at is not None
        assert session.committed

    def test_no_changes_skips_update(self):
        # The page's fields match what's already in the DB → updated=0.
        expense = _expense(
            title="Coffee",
            vendor="Starbucks",
            amount=Decimal("1198.0"),
            currency="JPY",
            transacted_at=datetime(2026, 6, 21, tzinfo=timezone.utc),
            category="Food",
            payment_method="Credit",
        )
        session = _FakeSession(expenses_by_page={"page-1": expense})
        puller, _ = _puller(session=session, query_pages=[_page()])

        stats = puller.sync_once()

        assert stats.updated == 0
        assert stats.skipped == 1
        # notion_synced_at must NOT advance on a no-op page — that
        # field tracks actual writes, not query observations.
        assert expense.notion_synced_at is None

    def test_orphan_page_logs_and_skips(self):
        session = _FakeSession(expenses_by_page={})  # no local row
        puller, _ = _puller(session=session, query_pages=[_page()])

        stats = puller.sync_once()

        assert stats.updated == 0
        assert stats.skipped == 1

    def test_cursor_advances_to_max_edited(self):
        # Two pages with different last_edited_time — cursor must end
        # at the latest one, regardless of input order.
        e1 = _expense(notion_page_id="page-a")
        e2 = _expense(notion_page_id="page-b")
        session = _FakeSession(
            expenses_by_page={"page-a": e1, "page-b": e2}
        )
        pages = [
            _page(page_id="page-a", edited="2026-06-30T01:00:00Z"),
            _page(page_id="page-b", edited="2026-06-30T05:00:00Z"),
        ]
        puller, _ = _puller(session=session, query_pages=pages)

        puller.sync_once()

        assert session.cursor is not None
        assert session.cursor.last_synced_at == datetime(
            2026, 6, 30, 5, 0, 0, tzinfo=timezone.utc
        )

    def test_cursor_not_rewound_on_empty_result(self):
        cursor_dt = datetime(2026, 6, 29, tzinfo=timezone.utc)
        cursor = NotionSyncState(
            database_id=DB_ID, last_synced_at=cursor_dt
        )
        session = _FakeSession(cursor=cursor)
        puller, _ = _puller(session=session, query_pages=[])

        puller.sync_once()

        assert session.cursor.last_synced_at == cursor_dt

    def test_paginates_through_has_more(self):
        e1 = _expense(notion_page_id="page-a")
        e2 = _expense(notion_page_id="page-b")
        session = _FakeSession(
            expenses_by_page={"page-a": e1, "page-b": e2}
        )
        batches = [
            [_page(page_id="page-a", edited="2026-06-30T01:00:00Z")],
            [_page(page_id="page-b", edited="2026-06-30T02:00:00Z")],
        ]
        puller, client = _puller(session=session, query_pages=batches)

        stats = puller.sync_once()

        assert stats.fetched == 2
        # First call: no start_cursor. Second call: start_cursor=cur-1.
        first_kwargs = client.databases.query.call_args_list[0].kwargs
        second_kwargs = client.databases.query.call_args_list[1].kwargs
        assert "start_cursor" not in first_kwargs
        assert second_kwargs["start_cursor"] == "cur-1"


# --- construction guards ---


class TestConstruction:
    def test_missing_token_raises(self):
        with pytest.raises(ValueError, match="NOTION_TOKEN"):
            NotionExpensePuller(
                _settings(token=""),
                _FakeSessionFactory(_FakeSession()),
                client=MagicMock(),
            )

    def test_missing_db_id_raises(self):
        with pytest.raises(
            ValueError, match="NOTION_EXPENSE_DATABASE_ID"
        ):
            NotionExpensePuller(
                _settings(expense_database_id=""),
                _FakeSessionFactory(_FakeSession()),
                client=MagicMock(),
            )
