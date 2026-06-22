"""email_analyses table

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "email_analyses",
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("importance", sa.String(length=16), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "analyzed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("message_id"),
    )


def downgrade() -> None:
    op.drop_table("email_analyses")
