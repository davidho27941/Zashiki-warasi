"""Notion Database mirror for expense records.

Maps an `ExpenseRecord` to a single page in the configured Notion
database. The integration is best-effort: callers in the expense
subgraph catch any exception from `record_expense` and surface it in
the SideEffect rather than failing the whole tick.
"""

from __future__ import annotations

import logging
from typing import Any

from notion_client import Client

from zashiki_warasi.core.config import NotionSettings
from zashiki_warasi.core.models import ExpenseRecord

logger = logging.getLogger(__name__)


class NotionSyncError(Exception):
    """Raised when the Notion API rejects the page-create request."""


class NotionExpenseRecorder:
    """Writes a row to the configured Notion expense database.

    Property names are Chinese — they MUST match the Notion database's
    schema exactly, including case and width. See README for the
    required schema.
    """

    # Property names — must match the Notion DB exactly.
    PROP_VENDOR = "消費店家"  # title
    PROP_AMOUNT = "消費金額"  # number
    PROP_CURRENCY = "幣別"  # select
    PROP_TRANSACTED_AT = "消費日期"  # date
    PROP_CATEGORY = "消費類別"  # select
    PROP_PAYMENT_METHOD = "支付方式"  # select
    PROP_TRANSACTION_ID = "UUID"  # rich_text

    # Translates our ISO-code Currency literal into the Chinese labels
    # the Notion DB uses for its `幣別` select options. If the user
    # extends Currency in the future they MUST add an entry here too.
    _CURRENCY_LABELS = {
        "JPY": "日幣",
        "TWD": "台幣",
        "USD": "美金",
    }

    def __init__(self, settings: NotionSettings | None = None) -> None:
        self._settings = settings or NotionSettings()
        if not self._settings.token:
            raise ValueError("NOTION_TOKEN is not set.")
        if not self._settings.expense_database_id:
            raise ValueError("NOTION_EXPENSE_DATABASE_ID is not set.")
        self._client = Client(
            auth=self._settings.token,
            timeout_ms=int(self._settings.timeout_seconds * 1000),
        )

    def record_expense(self, record: ExpenseRecord) -> str:
        """Create a page in the Notion database and return its id.

        Raises NotionSyncError on any API failure; callers should
        catch and route the message to the user's preferred fallback.
        """
        properties = self._build_properties(record)
        try:
            response = self._client.pages.create(
                parent={"database_id": self._settings.expense_database_id},
                properties=properties,
            )
        except Exception as exc:
            raise NotionSyncError(f"Notion page.create failed: {exc}") from exc

        page_id = response.get("id") if isinstance(response, dict) else None
        if not page_id:
            raise NotionSyncError(
                f"Notion page.create returned no id: {response!r}"
            )
        return page_id

    def _build_properties(self, record: ExpenseRecord) -> dict[str, Any]:
        """Translate an ExpenseRecord into Notion property payload.

        Title is always populated (with `(不明)` if vendor is None) —
        Notion requires the title property to exist. All other fields
        are skipped when None to keep the row clean.
        """
        properties: dict[str, Any] = {
            self.PROP_VENDOR: {
                "title": [
                    {"text": {"content": record.vendor or "(不明)"}}
                ],
            },
        }

        if record.amount is not None:
            properties[self.PROP_AMOUNT] = {"number": float(record.amount)}

        if record.currency:
            # ISO code → Chinese label expected by the Notion select.
            # If the LLM somehow extracts a currency we don't know
            # about, fall back to the raw code so the API at least
            # reports a clear "X is not an option" error rather than
            # silently dropping the field.
            label = self._CURRENCY_LABELS.get(
                record.currency, record.currency
            )
            properties[self.PROP_CURRENCY] = {
                "select": {"name": label},
            }

        if record.transacted_at is not None:
            properties[self.PROP_TRANSACTED_AT] = {
                "date": {"start": record.transacted_at.isoformat()},
            }

        if record.category:
            # Category is a Notion Select column. The LLM must extract
            # a label that exists as an option in the DB; new labels
            # need to be added manually in Notion.
            properties[self.PROP_CATEGORY] = {
                "select": {"name": record.category},
            }

        if record.payment_method:
            properties[self.PROP_PAYMENT_METHOD] = {
                "select": {"name": record.payment_method},
            }

        if record.transaction_id:
            properties[self.PROP_TRANSACTION_ID] = {
                "rich_text": [
                    {"text": {"content": record.transaction_id}}
                ],
            }

        # No location property in this schema — `record.location`
        # is kept in Postgres only.

        return properties
