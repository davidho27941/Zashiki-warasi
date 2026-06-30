"""Tests for thin wiring helpers in app.py."""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from zashiki_warasi import app
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


class TestMainCliRouting:
    """`main` (click command) — confirms --reset / --yes flow through
    to `reset_database` and that `run` is invoked / skipped correctly.

    All tests use `CliRunner.invoke`, which captures stdout, simulates
    `[y/N]` prompts via `input=`, and exposes the underlying exception
    as `result.exception` rather than propagating it."""

    @pytest.fixture(autouse=True)
    def _stub_dependencies(self, monkeypatch):
        # Replace the side-effectful pieces with mocks so the test
        # exercises only the CLI layer, not Postgres or the poller.
        self.mock_reset = MagicMock()
        self.mock_run = MagicMock()
        monkeypatch.setattr(app, "reset_database", self.mock_reset)
        monkeypatch.setattr(app, "run", self.mock_run)
        self.runner = CliRunner()

    def test_no_args_skips_reset(self):
        result = self.runner.invoke(app.main, [])
        assert result.exit_code == 0
        self.mock_reset.assert_not_called()
        self.mock_run.assert_called_once_with()

    def test_reset_with_y_response_calls_reset_and_run(self):
        result = self.runner.invoke(app.main, ["--reset"], input="y\n")
        assert result.exit_code == 0
        self.mock_reset.assert_called_once_with()
        self.mock_run.assert_called_once_with()

    def test_reset_with_n_response_aborts(self):
        # click.confirm(abort=True) exits with code 1 and does NOT
        # call reset_database or run.
        result = self.runner.invoke(app.main, ["--reset"], input="n\n")
        assert result.exit_code == 1
        self.mock_reset.assert_not_called()
        self.mock_run.assert_not_called()

    def test_reset_with_short_yes_skips_prompt(self):
        # `-y` provided → no input needed; if click DID prompt the
        # runner's empty input stream would EOF and fail.
        result = self.runner.invoke(app.main, ["--reset", "-y"])
        assert result.exit_code == 0
        self.mock_reset.assert_called_once_with()
        self.mock_run.assert_called_once_with()
        # Sanity: the confirmation question should not appear in
        # stdout when -y bypasses the prompt.
        assert "Continue?" not in result.output

    def test_reset_with_long_yes_also_skips_prompt(self):
        result = self.runner.invoke(app.main, ["--reset", "--yes"])
        assert result.exit_code == 0
        self.mock_reset.assert_called_once_with()

    def test_run_is_called_after_reset(self):
        order = []
        self.mock_reset.side_effect = lambda: order.append("reset")
        self.mock_run.side_effect = lambda: order.append("run")

        result = self.runner.invoke(app.main, ["--reset", "-y"])

        assert result.exit_code == 0
        assert order == ["reset", "run"]

    def test_help_shows_reset_flag(self):
        result = self.runner.invoke(app.main, ["--help"])
        assert result.exit_code == 0
        assert "--reset" in result.output
        assert "-y" in result.output

    def test_help_lists_sync_notion_subcommand(self):
        result = self.runner.invoke(app.main, ["--help"])
        assert result.exit_code == 0
        assert "sync-notion" in result.output

    def test_reset_with_subcommand_errors(self):
        # --reset only makes sense for the default (poller) invocation;
        # combining it with a subcommand must be a UsageError so users
        # don't expect both behaviours.
        result = self.runner.invoke(app.main, ["--reset", "sync-notion"])
        assert result.exit_code != 0
        assert "only valid without a subcommand" in result.output
        self.mock_reset.assert_not_called()
        self.mock_run.assert_not_called()


class TestSyncNotionCommand:
    """`sync-notion` subcommand — config gate + puller wiring."""

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        self.mock_puller_class = MagicMock()
        self.mock_puller = MagicMock()
        self.mock_puller.sync_once.return_value = "STATS_REPR"
        self.mock_puller_class.return_value = self.mock_puller
        monkeypatch.setattr(
            app, "NotionExpensePuller", self.mock_puller_class
        )
        # Cut session-factory dep so the test never reaches the DB.
        monkeypatch.setattr(
            app, "get_session_factory", lambda: MagicMock()
        )
        self.runner = CliRunner()

    def test_aborts_when_notion_not_configured(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "")
        monkeypatch.setenv("NOTION_EXPENSE_DATABASE_ID", "")
        result = self.runner.invoke(app.main, ["sync-notion"])
        assert result.exit_code != 0
        assert "NOTION_TOKEN" in result.output
        self.mock_puller_class.assert_not_called()

    def test_invokes_puller_when_configured(self, monkeypatch):
        monkeypatch.setenv("NOTION_TOKEN", "secret_xxx")
        monkeypatch.setenv("NOTION_EXPENSE_DATABASE_ID", "db-uuid")
        result = self.runner.invoke(app.main, ["sync-notion"])
        assert result.exit_code == 0
        self.mock_puller_class.assert_called_once()
        self.mock_puller.sync_once.assert_called_once_with()
        assert "STATS_REPR" in result.output
