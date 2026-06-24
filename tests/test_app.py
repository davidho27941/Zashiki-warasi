"""Tests for thin wiring helpers in app.py."""

from __future__ import annotations

from zashiki_warasi.app import _libpq_url


class TestLibpqUrl:
    def test_strips_psycopg_suffix(self):
        assert (
            _libpq_url("postgresql+psycopg://localhost/db")
            == "postgresql://localhost/db"
        )

    def test_preserves_credentials(self):
        assert (
            _libpq_url("postgresql+psycopg://user:pw@host:5432/db")
            == "postgresql://user:pw@host:5432/db"
        )

    def test_idempotent_on_already_libpq(self):
        assert (
            _libpq_url("postgresql://localhost/db")
            == "postgresql://localhost/db"
        )

    def test_only_replaces_first_occurrence(self):
        # Pathological: the literal substring "postgresql+psycopg://" appearing
        # somewhere else in the URL (e.g. embedded in a password) should not be
        # rewritten more than once.
        out = _libpq_url(
            "postgresql+psycopg://u:postgresql+psycopg://@host/db"
        )
        assert out == "postgresql://u:postgresql+psycopg://@host/db"

    def test_does_not_touch_non_postgres_urls(self):
        assert _libpq_url("sqlite:///x.db") == "sqlite:///x.db"
