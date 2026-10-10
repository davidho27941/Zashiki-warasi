"""Migration 0009 on real Postgres (adopt-email-taxonomy-v2, tasks 2.x).

Gated on TEST_DATABASE_URL — the EXCLUDE constraint, partial unique
index and view only exist on Postgres. The DSN's database is wiped
(public schema dropped) by these tests: never point it at real data.
"""

from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from zashiki_warasi.core.schemas import CATEGORIES_BY_VERSION

_PG_URL = os.environ.get("TEST_DATABASE_URL", "")
_ROOT = Path(__file__).resolve().parents[2]

pytestmark = pytest.mark.skipif(
    not _PG_URL.startswith("postgresql"),
    reason="TEST_DATABASE_URL not set to a Postgres DSN",
)

T0 = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _migration_module():
    path = _ROOT / "alembic" / "versions" / "0009_email_classifications.py"
    spec = importlib.util.spec_from_file_location("m0009", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def alembic_cfg(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", _PG_URL)
    # No ini file: alembic's env.py would otherwise run logging.fileConfig
    # and disable existing loggers, breaking caplog in later tests.
    cfg = Config()
    cfg.set_main_option("script_location", str(_ROOT / "alembic"))
    return cfg


@pytest.fixture()
def engine(alembic_cfg):
    eng = create_engine(_PG_URL)
    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    yield eng
    eng.dispose()


def _analysis(conn, message_id, category="消費支出", at=T0, version=None):
    cols = "message_id, importance, urgency, category, summary, keywords, analyzed_at"
    vals = ":m, 3, 'normal', :c, 's', '[]', :at"
    params = {"m": message_id, "c": category, "at": at}
    if version is not None:
        cols += ", taxonomy_version"
        vals += ", :v"
        params["v"] = version
    conn.execute(
        text(f"INSERT INTO email_analyses ({cols}) VALUES ({vals})"), params,
    )


def _classification(conn, message_id, valid_from, valid_to=None,
                    category="交易收據", version="v2", source="llm"):
    conn.execute(
        text(
            "INSERT INTO email_classifications (message_id, category, "
            "taxonomy_version, source, recorded_by, valid_from, valid_to) "
            "VALUES (:m, :c, :v, :s, 'app', :f, :t)"
        ),
        {"m": message_id, "c": category, "v": version, "s": source,
         "f": valid_from, "t": valid_to},
    )


def test_seed_and_view_on_upgrade(engine, alembic_cfg):
    command.upgrade(alembic_cfg, "0008")
    with engine.begin() as conn:
        _analysis(conn, "m1", "消費支出")
        _analysis(conn, "m2", "新聞", at=T0 + timedelta(days=1))
    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT message_id, category, is_exact, source_category, "
            "source_taxonomy_version, source, valid_from "
            "FROM email_category_current ORDER BY message_id"
        )).all()
        hist = conn.execute(text(
            "SELECT recorded_by FROM email_classifications"
        )).scalars().all()
    assert [tuple(r[:6]) for r in rows] == [
        ("m1", "交易收據", True, "消費支出", "v1", "llm"),
        ("m2", "內容電子報", False, "新聞", "v1", "llm"),
    ]
    assert rows[0].valid_from == T0  # seeded from analyzed_at
    assert set(hist) == {"migration"}


def test_seed_is_idempotent_and_picks_up_new_rows(engine, alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    seed = _migration_module().SEED_SQL
    with engine.begin() as conn:
        _analysis(conn, "m1")
        conn.execute(text(seed))
        _analysis(conn, "m2")
        conn.execute(text(seed))
        conn.execute(text(seed))
        counts = conn.execute(text(
            "SELECT message_id, count(*) FROM email_classifications "
            "GROUP BY 1 ORDER BY 1"
        )).all()
    assert [tuple(r) for r in counts] == [("m1", 1), ("m2", 1)]


def test_pre_v2_writer_gets_v1_default(engine, alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    with engine.begin() as conn:
        _analysis(conn, "old-code")  # no taxonomy_version column written
        version = conn.execute(text(
            "SELECT taxonomy_version FROM email_analyses"
        )).scalar_one()
    assert version == "v1"


def test_downgrade_and_reupgrade(engine, alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    command.downgrade(alembic_cfg, "0008")
    with engine.connect() as conn:
        tables = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public'"
        )).scalars())
    assert "email_classifications" not in tables
    assert "taxonomy_mappings" not in tables
    assert "email_category_current" not in tables
    command.upgrade(alembic_cfg, "head")


def test_mappings_cover_every_category(engine, alembic_cfg):
    command.upgrade(alembic_cfg, "head")
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT from_version, from_category, to_category, is_exact "
            "FROM taxonomy_mappings WHERE mapping_rev = 1 AND to_version = 'v2'"
        )).all()
    by_version: dict[str, set[str]] = {}
    for r in rows:
        by_version.setdefault(r.from_version, set()).add(r.from_category)
        assert r.to_category in CATEGORIES_BY_VERSION["v2"]
    assert by_version["v1"] == set(CATEGORIES_BY_VERSION["v1"])
    assert by_version["v2"] == set(CATEGORIES_BY_VERSION["v2"])
    exact_v1 = {r.from_category for r in rows
                if r.from_version == "v1" and r.is_exact}
    assert exact_v1 == {"消費支出", "促銷", "會議邀請", "技術文章",
                        "問卷調查", "社交"}


class TestConstraints:
    @pytest.fixture()
    def conn(self, engine, alembic_cfg):
        command.upgrade(alembic_cfg, "head")
        with engine.begin() as c:
            _analysis(c, "m1", version="v2", category="交易收據")
        with engine.connect() as c:
            yield c

    def test_second_current_row_rejected(self, conn):
        _classification(conn, "m1", T0)
        with pytest.raises(IntegrityError):
            _classification(conn, "m1", T0 + timedelta(hours=1))

    def test_overlapping_range_rejected(self, conn):
        _classification(conn, "m1", T0, T0 + timedelta(days=2))
        with pytest.raises(IntegrityError):
            _classification(
                conn, "m1", T0 + timedelta(days=1), T0 + timedelta(days=3),
            )

    def test_adjacent_ranges_allowed(self, conn):
        _classification(conn, "m1", T0, T0 + timedelta(days=1))
        _classification(conn, "m1", T0 + timedelta(days=1))

    def test_empty_or_inverted_range_rejected(self, conn):
        with pytest.raises(IntegrityError):
            _classification(conn, "m1", T0, T0)

    def test_unknown_source_rejected(self, conn):
        with pytest.raises(IntegrityError):
            _classification(conn, "m1", T0, source="rule")

    def test_unknown_message_rejected(self, conn):
        with pytest.raises(IntegrityError):
            _classification(conn, "no-such-message", T0)
