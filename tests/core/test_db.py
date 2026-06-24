"""Engine / session-factory singleton behaviour."""

from __future__ import annotations

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
