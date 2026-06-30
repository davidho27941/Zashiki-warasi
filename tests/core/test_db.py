"""Engine / session-factory singleton behaviour."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from zashiki_warasi.core import db


@pytest.fixture(autouse=True)
def _clear_lru_caches():
    """Reset cached singletons before and after each test so env-var
    overrides set with monkeypatch actually take effect."""
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()
    yield
    db.get_engine.cache_clear()
    db.get_session_factory.cache_clear()


@pytest.fixture
def sqlite_url(monkeypatch):
    """Point DatabaseSettings at an in-memory SQLite to keep the tests
    hermetic — they only need a real Engine, not Postgres."""
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")


class TestGetEngine:
    def test_returns_sqlalchemy_engine(self, sqlite_url):
        engine = db.get_engine()
        assert isinstance(engine, Engine)

    def test_is_singleton(self, sqlite_url):
        first = db.get_engine()
        second = db.get_engine()
        assert first is second

    def test_url_comes_from_database_settings(self, sqlite_url):
        engine = db.get_engine()
        assert str(engine.url) == "sqlite+pysqlite:///:memory:"


class TestGetSessionFactory:
    def test_returns_sessionmaker(self, sqlite_url):
        factory = db.get_session_factory()
        assert isinstance(factory, sessionmaker)

    def test_is_singleton(self, sqlite_url):
        first = db.get_session_factory()
        second = db.get_session_factory()
        assert first is second

    def test_bound_to_cached_engine(self, sqlite_url):
        factory = db.get_session_factory()
        engine = db.get_engine()
        assert factory.kw["bind"] is engine

    def test_yields_usable_sessions(self, sqlite_url):
        factory = db.get_session_factory()
        with factory() as session:
            assert isinstance(session, Session)

    def test_expire_on_commit_disabled(self, sqlite_url):
        factory = db.get_session_factory()
        assert factory.kw["expire_on_commit"] is False


# --- reset_database ---


def _mock_engine_with_existing_tables(existing: list[str]) -> MagicMock:
    """Build a MagicMock that quacks like an Engine: `begin()` returns a
    context manager whose entered connection serves the
    `information_schema` query and accepts the subsequent TRUNCATE."""
    conn = MagicMock()
    # First execute() = SELECT against information_schema; iterating its
    # result yields (table_name,) one-tuples. Second execute() = TRUNCATE
    # which the caller never iterates, so a default MagicMock suffices.
    conn.execute.side_effect = [
        [(name,) for name in existing],
        MagicMock(),
    ]

    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value = conn
    engine.begin.return_value.__exit__.return_value = False
    # Attach the conn for downstream assertions.
    engine._test_conn = conn
    return engine


class TestResetDatabaseTruncate:
    """The actual TRUNCATE call — table discovery and SQL shape.

    `reset_database` itself unconditionally truncates; the CLI layer
    in `app.py` owns the confirmation prompt (covered separately in
    `tests/test_app.py::TestMainCliRouting`)."""

    def test_empty_database_skips_truncate(self, monkeypatch):
        engine = _mock_engine_with_existing_tables([])
        monkeypatch.setattr(db, "get_engine", lambda: engine)

        db.reset_database()

        # Only the information_schema SELECT — no TRUNCATE issued.
        assert engine._test_conn.execute.call_count == 1

    def test_truncates_only_existing_tables(self, monkeypatch):
        # LangGraph checkpoint tables not yet created (fresh install) —
        # we should TRUNCATE only the app tables that exist.
        existing = [
            "gmail_sync_state",
            "processed_messages",
            "email_analyses",
            "expenses",
        ]
        engine = _mock_engine_with_existing_tables(existing)
        monkeypatch.setattr(db, "get_engine", lambda: engine)

        db.reset_database()

        # Two execute() calls: discovery + TRUNCATE.
        assert engine._test_conn.execute.call_count == 2
        truncate_sql = str(engine._test_conn.execute.call_args_list[1].args[0])
        for table in existing:
            assert f'"{table}"' in truncate_sql
        assert "TRUNCATE TABLE" in truncate_sql
        assert "CASCADE" in truncate_sql

    def test_includes_checkpoint_tables_when_present(self, monkeypatch):
        # Post-first-run scenario: LangGraph has materialised its tables.
        existing = [
            "gmail_sync_state",
            "processed_messages",
            "email_analyses",
            "expenses",
            "checkpoints",
            "checkpoint_writes",
            "checkpoint_blobs",
            "checkpoint_migrations",
        ]
        engine = _mock_engine_with_existing_tables(existing)
        monkeypatch.setattr(db, "get_engine", lambda: engine)

        db.reset_database()

        truncate_sql = str(engine._test_conn.execute.call_args_list[1].args[0])
        for table in existing:
            assert f'"{table}"' in truncate_sql

    def test_discovery_query_passes_full_target_list(self, monkeypatch):
        # Whitelist of names sent to information_schema must match the
        # module's _RESET_TARGET_TABLES constant — otherwise a newly-
        # added domain table won't get cleared.
        engine = _mock_engine_with_existing_tables([])
        monkeypatch.setattr(db, "get_engine", lambda: engine)

        db.reset_database()

        discovery_kwargs = engine._test_conn.execute.call_args_list[0].args[1]
        assert set(discovery_kwargs["names"]) == set(db._RESET_TARGET_TABLES)
