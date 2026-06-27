"""expenses: add title (LLM-generated expense name)

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: Union[str, Sequence[str], None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column("title", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("expenses", "title")
