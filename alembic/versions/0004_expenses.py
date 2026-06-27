"""expenses table for the expense vertical

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: Union[str, Sequence[str], None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "expenses",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "message_id", sa.String(length=64), nullable=False,
        ),
        sa.Column("amount", sa.Numeric(12, 2), nullable=True),
        sa.Column("currency", sa.String(length=8), nullable=True),
        sa.Column(
            "transacted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("vendor", sa.String(length=256), nullable=True),
        sa.Column("location", sa.String(length=512), nullable=True),
        sa.Column("category", sa.String(length=64), nullable=True),
        sa.Column(
            "transaction_id", sa.String(length=128), nullable=True,
        ),
        sa.Column(
            "payment_method", sa.String(length=32), nullable=True,
        ),
        sa.Column(
            "raw_extraction",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("message_id", name="uq_expenses_message_id"),
    )


def downgrade() -> None:
    op.drop_table("expenses")
