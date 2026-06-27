"""NotionExpenseRecorder: construction guards, property mapping, errors."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from zashiki_warasi.core.config import NotionSettings
from zashiki_warasi.core.models import ExpenseRecord
from zashiki_warasi.notifications.notion import (
    NotionExpenseRecorder,
    NotionSyncError,
)


def _settings(**overrides) -> NotionSettings:
    base = dict(
        token="secret_xxx",
        expense_database_id="db-uuid-abc",
        timeout_seconds=5.0,
    )
    base.update(overrides)
    return NotionSettings(**base)


def _recorder_with_mock_client(
    response: dict | None = None,
    *,
    raises: Exception | None = None,
) -> NotionExpenseRecorder:
    """Build a NotionExpenseRecorder whose underlying client is mocked.

    Bypasses __init__'s real `notion_client.Client(...)` instantiation
    so tests don't depend on the package's transport layer.
    """
    recorder = NotionExpenseRecorder.__new__(NotionExpenseRecorder)
    recorder._settings = _settings()
    recorder._client = MagicMock()
    if raises is not None:
        recorder._client.pages.create.side_effect = raises
    else:
        recorder._client.pages.create.return_value = response or {
            "id": "page-uuid-from-notion"
        }
    return recorder


def _record(**overrides) -> ExpenseRecord:
    base = dict(
        id=uuid.uuid4(),
        message_id="m-1",
        amount=Decimal("1198"),
        currency="JPY",
        transacted_at=datetime(2026, 6, 21, 15, 13, 3, tzinfo=timezone.utc),
        vendor="Starbucks",
        location="東京都渋谷区",
        category="餐飲",
        transaction_id="303840",
        payment_method="SMBC Olive",
        raw_extraction={},
        notion_page_id=None,
        notion_sync_error=None,
    )
    base.update(overrides)
    return ExpenseRecord(**base)


# --- construction ---


class TestConstruction:
    def test_rejects_empty_token(self):
        with pytest.raises(ValueError, match="NOTION_TOKEN"):
            NotionExpenseRecorder(_settings(token=""))

    def test_rejects_empty_database_id(self):
        with pytest.raises(ValueError, match="NOTION_EXPENSE_DATABASE_ID"):
            NotionExpenseRecorder(_settings(expense_database_id=""))


# --- record_expense: parent + response ---


class TestRequestShape:
    def test_passes_database_id_as_parent(self):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record())

        call = recorder._client.pages.create.call_args
        assert call.kwargs["parent"] == {"database_id": "db-uuid-abc"}

    def test_returns_page_id_from_response(self):
        recorder = _recorder_with_mock_client(
            response={"id": "concrete-page-id"}
        )
        assert recorder.record_expense(_record()) == "concrete-page-id"


# --- property building (full record) ---


class TestPropertyMapping:
    def test_vendor_becomes_title(self):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(vendor="Coffee Place"))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["消費店家"] == {
            "title": [{"text": {"content": "Coffee Place"}}]
        }

    def test_none_vendor_falls_back_to_buming_title(self):
        # Notion requires the title to exist; we never send a missing
        # title even when the LLM couldn't extract a vendor.
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(vendor=None))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["消費店家"]["title"][0]["text"]["content"] == "(不明)"

    def test_amount_is_float(self):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(amount=Decimal("3200")))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        # Notion's number type expects a JSON number, not a Decimal.
        assert props["消費金額"] == {"number": 3200.0}
        assert isinstance(props["消費金額"]["number"], float)

    @pytest.mark.parametrize(
        "iso_code,chinese_label",
        [("JPY", "日幣"), ("TWD", "台幣"), ("USD", "美金")],
    )
    def test_currency_is_translated_to_chinese_select(
        self, iso_code, chinese_label
    ):
        # Currency is stored internally as the ISO code but the Notion
        # select uses Chinese labels — the recorder translates.
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(currency=iso_code))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["幣別"] == {"select": {"name": chinese_label}}

    def test_unknown_currency_falls_back_to_raw_code(self):
        # Defensive: if Currency literal gains a new value but the
        # translation map isn't updated, we send the raw code and let
        # Notion's API surface a clear "X is not an option" error.
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(currency="EUR"))  # not mapped

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["幣別"] == {"select": {"name": "EUR"}}

    def test_payment_method_is_select(self):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(payment_method="信用卡"))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["支付方式"] == {"select": {"name": "信用卡"}}

    def test_category_is_select(self):
        # Category is a Notion Select column; the LLM-extracted label
        # must already exist as an option in the Notion DB or the API
        # will reject the request.
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(category="飲食"))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["消費類別"] == {"select": {"name": "飲食"}}

    def test_transacted_at_is_iso_date(self):
        recorder = _recorder_with_mock_client()
        record = _record(
            transacted_at=datetime(2026, 6, 21, 15, 13, 3, tzinfo=timezone.utc)
        )
        recorder.record_expense(record)

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["消費日期"]["date"]["start"].startswith(
            "2026-06-21T15:13:03"
        )

    def test_transaction_id_is_rich_text(self):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(transaction_id="ORDER-12345"))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert props["UUID"] == {
            "rich_text": [{"text": {"content": "ORDER-12345"}}]
        }

    def test_location_is_not_sent_to_notion(self):
        # The user's schema deliberately omits a location property —
        # the field is kept in Postgres only.
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(location="東京都渋谷区"))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert "地點" not in props
        assert "location" not in props


# --- property building: skip None fields ---


class TestNullableFieldSkipped:
    @pytest.mark.parametrize(
        "field,prop_key",
        [
            ("amount", "消費金額"),
            ("currency", "幣別"),
            ("transacted_at", "消費日期"),
            ("category", "消費類別"),
            ("payment_method", "支付方式"),
            ("transaction_id", "UUID"),
        ],
    )
    def test_none_field_omitted_from_properties(self, field, prop_key):
        recorder = _recorder_with_mock_client()
        recorder.record_expense(_record(**{field: None}))

        props = recorder._client.pages.create.call_args.kwargs["properties"]
        assert prop_key not in props


# --- error handling ---


class TestErrorMapping:
    def test_api_exception_wrapped_in_notion_sync_error(self):
        recorder = _recorder_with_mock_client(
            raises=RuntimeError("connection reset")
        )

        with pytest.raises(NotionSyncError, match="connection reset"):
            recorder.record_expense(_record())

    def test_response_without_id_raises(self):
        recorder = _recorder_with_mock_client(response={"object": "page"})
        # Notion responses normally include id; missing id is an
        # integration / library issue we should surface loudly.
        with pytest.raises(NotionSyncError, match="returned no id"):
            recorder.record_expense(_record())
