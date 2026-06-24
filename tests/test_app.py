"""Tests for thin wiring helpers in app.py."""

from __future__ import annotations

import signal
import threading

import pytest

from zashiki_warasi.app import _install_shutdown_handlers, _libpq_url


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


class TestInstallShutdownHandlers:
    """Verifies the SIGINT/SIGTERM -> stop_event glue.

    Saves and restores the original signal handlers so the rest of the
    test suite (and pytest's own signal handling) is unaffected.
    """

    @pytest.fixture(autouse=True)
    def _preserve_handlers(self):
        original_int = signal.getsignal(signal.SIGINT)
        original_term = signal.getsignal(signal.SIGTERM)
        yield
        signal.signal(signal.SIGINT, original_int)
        signal.signal(signal.SIGTERM, original_term)

    def test_registers_handlers_for_both_signals(self):
        event = threading.Event()
        _install_shutdown_handlers(event)

        assert signal.getsignal(signal.SIGINT) is not signal.default_int_handler
        assert signal.getsignal(signal.SIGTERM) is not signal.SIG_DFL

    def test_sigterm_sets_event_once(self):
        event = threading.Event()
        _install_shutdown_handlers(event)
        handler = signal.getsignal(signal.SIGTERM)

        handler(signal.SIGTERM, None)

        assert event.is_set()

    def test_first_sigint_sets_event_without_raising(self):
        event = threading.Event()
        _install_shutdown_handlers(event)
        handler = signal.getsignal(signal.SIGINT)

        handler(signal.SIGINT, None)  # must NOT raise

        assert event.is_set()

    def test_second_sigint_raises_keyboard_interrupt(self):
        event = threading.Event()
        _install_shutdown_handlers(event)
        handler = signal.getsignal(signal.SIGINT)

        handler(signal.SIGINT, None)  # first: graceful
        with pytest.raises(KeyboardInterrupt):
            handler(signal.SIGINT, None)  # second: force

    def test_second_sigint_restores_default_handler(self):
        event = threading.Event()
        _install_shutdown_handlers(event)
        handler = signal.getsignal(signal.SIGINT)

        handler(signal.SIGINT, None)
        try:
            handler(signal.SIGINT, None)
        except KeyboardInterrupt:
            pass

        assert signal.getsignal(signal.SIGINT) is signal.default_int_handler

    def test_sigterm_does_not_use_sigint_force_path(self):
        """Two SIGTERMs in a row should just keep setting the event,
        never raise — only Ctrl+C gets the press-twice-to-force semantic."""
        event = threading.Event()
        _install_shutdown_handlers(event)
        handler = signal.getsignal(signal.SIGTERM)

        handler(signal.SIGTERM, None)
        handler(signal.SIGTERM, None)  # must not raise

        assert event.is_set()
