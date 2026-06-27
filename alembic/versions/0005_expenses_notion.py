"""expenses: add notion_page_id and notion_sync_error

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: Union[str, Sequence[str], None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column("notion_page_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "expenses",
        sa.Column("notion_sync_error", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expenses", "notion_sync_error")
    op.drop_column("expenses", "notion_page_id")
