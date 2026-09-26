"""laya_shadow_predictions table (v1.6.0 add-laya-shadow-classifier)

One row per (email, laya model version): the exact truncated input
text, both engines' categories, confidence / alternates / latency /
error class. Purely additive; downgrade drops the table.

Revision ID: 0008
Revises: 0007
Create Date: 2026-09-26

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0008"
down_revision: Union[str, Sequence[str], None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "laya_shadow_predictions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("input_text", sa.Text(), nullable=False),
        sa.Column("llm_category", sa.String(length=32), nullable=False),
        sa.Column("laya_category", sa.String(length=32), nullable=True),
        sa.Column("laya_confidence", sa.Float(), nullable=True),
        sa.Column(
            "laya_alternates",
            sa.JSON().with_variant(JSONB(), "postgresql"),
            nullable=True,
        ),
        sa.Column("laya_latency_ms", sa.Integer(), nullable=True),
        sa.Column("laya_error", sa.String(length=32), nullable=True),
        sa.Column("laya_model_ver", sa.String(length=64), nullable=False),
        sa.Column(
            "predicted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "message_id",
            "laya_model_ver",
            name="uq_laya_shadow_message_model",
        ),
    )
    op.create_index(
        "ix_laya_shadow_llm_category",
        "laya_shadow_predictions",
        ["llm_category"],
    )
    op.create_index(
        "ix_laya_shadow_laya_category",
        "laya_shadow_predictions",
        ["laya_category"],
    )
    op.create_index(
        "ix_laya_shadow_predicted_at",
        "laya_shadow_predictions",
        ["predicted_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_laya_shadow_predicted_at", table_name="laya_shadow_predictions"
    )
    op.drop_index(
        "ix_laya_shadow_laya_category", table_name="laya_shadow_predictions"
    )
    op.drop_index(
        "ix_laya_shadow_llm_category", table_name="laya_shadow_predictions"
    )
    op.drop_table("laya_shadow_predictions")
