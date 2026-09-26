"""LayaShadowPrediction model + migration-shape tests.

Two tiers:

- SQLite tier (runs everywhere): `Base.metadata.create_all` must keep
  working with the new table in the metadata — this is the regression
  guard for the `JSON().with_variant(JSONB, "postgresql")` choice; a
  bare-JSONB column would break the ~800 existing SQLite-based tests
  at create_all.
- Postgres tier (gated on `TEST_DATABASE_URL`): conflict-ignore on the
  (message_id, laya_model_ver) unique constraint + JSONB round-trip —
  the behaviors the shadow client's persistence path relies on. Skipped
  when no Postgres is provided (operator manages their own instance).
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from zashiki_warasi.core.models import Base, LayaShadowPrediction

_PG_URL = os.environ.get("TEST_DATABASE_URL", "")

requires_postgres = pytest.mark.skipif(
    not _PG_URL.startswith("postgresql"),
    reason="TEST_DATABASE_URL not set to a Postgres DSN",
)


class TestSqliteMetadataCompat:
    def test_create_all_still_works_on_sqlite(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)  # must not raise on JSONB variant

    def test_basic_insert_and_readback_on_sqlite(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        with factory() as session:
            session.add(
                LayaShadowPrediction(
                    message_id="m1",
                    input_text="主旨\n\n內文",
                    llm_category="廣告",
                    laya_category="促銷",
                    laya_confidence=0.72,
                    laya_alternates=[
                        {"cat": "廣告", "prob": 0.21},
                        {"cat": "社交", "prob": 0.05},
                    ],
                    laya_latency_ms=213,
                    laya_model_ver="v0.1-abcd1234",
                )
            )
            session.commit()
            row = session.scalar(select(LayaShadowPrediction))
            assert row.input_text == "主旨\n\n內文"
            assert row.laya_alternates[0]["cat"] == "廣告"
            assert row.predicted_at is not None


@requires_postgres
class TestPostgresBehavior:
    @pytest.fixture()
    def pg_session_factory(self):
        engine = create_engine(_PG_URL)
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False)
        yield factory
        with engine.begin() as conn:
            conn.execute(LayaShadowPrediction.__table__.delete())
        engine.dispose()

    def test_conflict_ignore_on_duplicate(self, pg_session_factory):
        values = dict(
            message_id="dup-1",
            input_text="text",
            llm_category="廣告",
            laya_category="廣告",
            laya_model_ver="v0.1-ffff0000",
        )
        with pg_session_factory() as session:
            stmt = (
                pg_insert(LayaShadowPrediction)
                .values(**values)
                .on_conflict_do_nothing(
                    constraint="uq_laya_shadow_message_model"
                )
            )
            session.execute(stmt)
            session.execute(stmt)  # second insert must be a silent no-op
            session.commit()
            rows = session.scalars(
                select(LayaShadowPrediction).where(
                    LayaShadowPrediction.message_id == "dup-1"
                )
            ).all()
            assert len(rows) == 1

    def test_jsonb_roundtrip(self, pg_session_factory):
        alternates = [{"cat": "講座資訊", "prob": 0.21}]
        with pg_session_factory() as session:
            session.add(
                LayaShadowPrediction(
                    message_id="jsonb-1",
                    input_text="t",
                    llm_category="講座資訊",
                    laya_category="廣告",
                    laya_alternates=alternates,
                    laya_model_ver="v0.1-ffff0000",
                )
            )
            session.commit()
            row = session.scalar(
                select(LayaShadowPrediction).where(
                    LayaShadowPrediction.message_id == "jsonb-1"
                )
            )
            assert row.laya_alternates == alternates
