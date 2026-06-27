"""analysis schema v2: importance INT, urgency, keywords

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-27

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, Sequence[str], None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Wipe existing rows before reshaping. The schema change is not
    # cleanly migratable (importance VARCHAR → INTEGER, plus two new
    # NOT NULL columns with no semantically correct backfill value),
    # and per the v0.3 CHANGELOG entry these are dev-only rows.
    # Without the TRUNCATE the ADD COLUMN ... NOT NULL below would
    # raise NotNullViolation on any non-empty table.
    op.execute("TRUNCATE TABLE email_analyses")

    # importance: VARCHAR(16) → INTEGER
    op.drop_column("email_analyses", "importance")
    op.add_column(
        "email_analyses",
        sa.Column("importance", sa.Integer(), nullable=False),
    )

    op.add_column(
        "email_analyses",
        sa.Column("urgency", sa.String(length=16), nullable=False),
    )

    op.add_column(
        "email_analyses",
        sa.Column(
            "keywords",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    op.drop_column("email_analyses", "keywords")
    op.drop_column("email_analyses", "urgency")
    op.drop_column("email_analyses", "importance")
    op.add_column(
        "email_analyses",
        sa.Column(
            "importance",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
    )
