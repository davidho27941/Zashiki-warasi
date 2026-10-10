"""email_classifications SCD-2 history + taxonomy mappings (taxonomy v2)

openspec `adopt-email-taxonomy-v2`:

- `taxonomy_version` on `email_analyses` and `laya_shadow_predictions`
  (server default 'v1': pre-v2 code keeps writing during the rolling
  deploy and after an image rollback, and everything it writes is v1).
- `email_classifications`: transaction-time SCD type 2. Current row
  is `valid_to IS NULL`; Postgres enforces one current row per message
  (partial unique) and no overlapping ranges (EXCLUDE, btree_gist).
- `taxonomy_mappings`: append-only, rev 1 = every v1 category → v2
  plus v2 identities. `is_exact` only where no v1 row of the class is
  known to belong elsewhere under v2 rules.
- `email_category_current`: the single read path — current row mapped
  to the newest taxonomy through the pinned rev.
- Seed: one v1 history row per existing analysis (`source='llm'`,
  `recorded_by='migration'`, `valid_from=analyzed_at`). Idempotent;
  `scripts/seed_classifications.py` re-runs the same statement after
  rollout to cover analyses the old pod wrote in between.

Data is hard-coded here (not imported from the app) so the migration
stays frozen when the taxonomy module evolves.

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-11

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: Union[str, Sequence[str], None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

MAPPING_REV = 1

# (from_category, to_category, is_exact) for v1 → v2.
V1_TO_V2: tuple[tuple[str, str, bool], ...] = (
    ("消費支出", "交易收據", True),
    ("促銷", "行銷", True),
    ("會議邀請", "行事曆事件", True),
    ("技術文章", "內容電子報", True),
    ("問卷調查", "問卷調查", True),
    ("社交", "社交", True),
    ("廣告", "行銷", False),
    ("講座資訊", "行事曆事件", False),
    ("點數資訊彙整", "帳務彙整", False),
    ("消費資訊彙整", "帳務彙整", False),
    ("帳單通知", "帳單通知", False),
    ("新聞", "內容電子報", False),
    ("安全通知", "安全通知", False),
    ("股票資訊", "投資帳戶通知", False),
    ("訂閱服務", "服務通知", False),
    ("其他", "其他", False),
)

V2_CATEGORIES: tuple[str, ...] = (
    "交易收據", "帳務彙整", "帳單通知", "服務通知", "行事曆事件",
    "安全通知", "公共警報", "投資帳戶通知", "內容電子報", "行銷",
    "社交", "職涯", "問卷調查", "其他",
)

SEED_SQL = """
INSERT INTO email_classifications
    (message_id, category, taxonomy_version, source, recorded_by, valid_from)
SELECT a.message_id, a.category, a.taxonomy_version, 'llm', 'migration',
       a.analyzed_at
FROM email_analyses a
WHERE NOT EXISTS (
    SELECT 1 FROM email_classifications c WHERE c.message_id = a.message_id
)
"""

VIEW_SQL = f"""
CREATE VIEW email_category_current AS
SELECT c.message_id,
       m.to_category      AS category,
       m.is_exact,
       c.category         AS source_category,
       c.taxonomy_version AS source_taxonomy_version,
       c.source,
       c.valid_from
FROM email_classifications c
LEFT JOIN taxonomy_mappings m
       ON m.mapping_rev = {MAPPING_REV}
      AND m.from_version = c.taxonomy_version
      AND m.from_category = c.category
      AND m.to_version = 'v2'
WHERE c.valid_to IS NULL
"""


def upgrade() -> None:
    for table in ("email_analyses", "laya_shadow_predictions"):
        op.add_column(
            table,
            sa.Column(
                "taxonomy_version",
                sa.String(length=8),
                nullable=False,
                server_default="v1",
            ),
        )

    op.create_table(
        "email_classifications",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("message_id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("taxonomy_version", sa.String(length=8), nullable=False),
        sa.Column("source", sa.String(length=8), nullable=False),
        sa.Column("recorded_by", sa.String(length=16), nullable=False),
        sa.Column("valid_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["message_id"], ["email_analyses.message_id"],
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to > valid_from",
            name="ck_email_classifications_valid_range",
        ),
        sa.CheckConstraint(
            "source IN ('llm', 'laya', 'human')",
            name="ck_email_classifications_source",
        ),
        sa.CheckConstraint(
            "recorded_by IN ('app', 'migration', 'import')",
            name="ck_email_classifications_recorded_by",
        ),
    )
    op.create_index(
        "uq_email_classifications_current",
        "email_classifications",
        ["message_id"],
        unique=True,
        postgresql_where=sa.text("valid_to IS NULL"),
    )

    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
        op.execute(
            "ALTER TABLE email_classifications "
            "ADD CONSTRAINT ex_email_classifications_no_overlap "
            "EXCLUDE USING gist ("
            "message_id WITH =, "
            "tstzrange(valid_from, valid_to, '[)') WITH &&)"
        )

    mappings = op.create_table(
        "taxonomy_mappings",
        sa.Column("mapping_rev", sa.Integer(), nullable=False),
        sa.Column("from_version", sa.String(length=8), nullable=False),
        sa.Column("from_category", sa.String(length=32), nullable=False),
        sa.Column("to_version", sa.String(length=8), nullable=False),
        sa.Column("to_category", sa.String(length=32), nullable=False),
        sa.Column("is_exact", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "mapping_rev", "from_version", "from_category", "to_version",
        ),
    )
    rows = [
        dict(mapping_rev=MAPPING_REV, from_version="v1", from_category=src,
             to_version="v2", to_category=dst, is_exact=exact)
        for src, dst, exact in V1_TO_V2
    ] + [
        dict(mapping_rev=MAPPING_REV, from_version="v2", from_category=c,
             to_version="v2", to_category=c, is_exact=True)
        for c in V2_CATEGORIES
    ]
    op.bulk_insert(mappings, rows)

    op.execute(VIEW_SQL)
    op.execute(SEED_SQL)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS email_category_current")
    op.drop_table("taxonomy_mappings")
    op.drop_index(
        "uq_email_classifications_current",
        table_name="email_classifications",
    )
    op.drop_table("email_classifications")
    # btree_gist is left installed: harmless, and other objects may use it.
    for table in ("laya_shadow_predictions", "email_analyses"):
        op.drop_column(table, "taxonomy_version")
