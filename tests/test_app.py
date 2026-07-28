"""Tests for thin wiring helpers in app.py."""

from __future__ import annotations

import signal
import threading
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from zashiki_warasi import app
from zashiki_warasi.app import (
    EXIT_CREDENTIAL_FAILURE,
    _init_logging,
    _install_shutdown_handlers,
    _libpq_url,
)
from zashiki_warasi.gmail.exceptions import CredentialRefreshError


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


class TestInitLogging:
    """Bootstrap must delegate to configure_logging so the whole app
    gets the ContextFormatter, third-party level suppression, and
    env-driven levels — not just the old bare basicConfig."""

    def test_init_logging_calls_configure_logging(self, monkeypatch):
        spy = MagicMock()
        monkeypatch.setattr("zashiki_warasi.app.configure_logging", spy)
        _init_logging()
        spy.assert_called_once_with()


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


class TestReauthCommand:
    """`reauth` subcommand — nukes token.json and re-runs the OAuth flow."""

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch, tmp_path):
        self.token_path = tmp_path / "token.json"
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(self.token_path))
        monkeypatch.setenv(
            "GMAIL_CREDENTIALS_PATH", str(tmp_path / "credentials.json")
        )
        (tmp_path / "credentials.json").write_text('{"installed": {}}')
        self.mock_get_credentials = MagicMock()
        # Default: return credentials with a fresh refresh token.
        fresh = MagicMock()
        fresh.refresh_token = "new-rt"
        self.mock_get_credentials.return_value = fresh
        monkeypatch.setattr(app, "get_credentials", self.mock_get_credentials)
        self.runner = CliRunner()

    def test_removes_existing_token_before_reauth(self):
        self.token_path.write_text("stale")

        result = self.runner.invoke(app.main, ["reauth"])

        assert result.exit_code == 0
        assert not self.token_path.exists() or self.token_path.read_text() != "stale"
        self.mock_get_credentials.assert_called_once()
        assert "Removed stale token" in result.output
        assert "Re-authorised" in result.output

    def test_runs_flow_even_when_no_token_exists(self):
        assert not self.token_path.exists()

        result = self.runner.invoke(app.main, ["reauth"])

        assert result.exit_code == 0
        self.mock_get_credentials.assert_called_once()
        assert "No cached token" in result.output

    def test_warns_when_new_credentials_have_no_refresh_token(self):
        no_rt = MagicMock()
        no_rt.refresh_token = None
        self.mock_get_credentials.return_value = no_rt

        result = self.runner.invoke(app.main, ["reauth"])

        assert result.exit_code == 0
        # click.echo(err=True) goes to stderr, which mixes into
        # result.output for CliRunner unless mix_stderr=False. Default
        # mixes them, so search the full output.
        assert "WARNING" in result.output
        assert "refresh_token" in result.output

    def test_exits_nonzero_when_flow_raises(self):
        self.mock_get_credentials.side_effect = FileNotFoundError(
            "credentials.json missing"
        )

        result = self.runner.invoke(app.main, ["reauth"])

        assert result.exit_code != 0

    def test_help_lists_reauth_subcommand(self):
        result = self.runner.invoke(app.main, ["--help"])
        assert result.exit_code == 0
        assert "reauth" in result.output


class TestRunStartupCredentialFailure:
    """`run()` must catch CredentialRefreshError from get_credentials,
    notify Telegram (best-effort), and sys.exit non-zero — not crash
    silently or trigger a poller loop with a dead client."""

    @pytest.fixture(autouse=True)
    def _stub(self, monkeypatch):
        self.mock_notifier = MagicMock()
        monkeypatch.setattr(
            app, "TelegramNotifier", lambda: self.mock_notifier
        )
        monkeypatch.setattr(
            app, "get_session_factory", lambda: MagicMock()
        )
        # Stub these so they can never be reached — if they are, the
        # test's exit assertion will fail loudly rather than mysteriously.
        self.mock_gmail_client = MagicMock()
        monkeypatch.setattr(app, "GmailClient", self.mock_gmail_client)
        monkeypatch.setattr(app, "_build_notion", lambda: None)
        monkeypatch.setattr(
            app, "_start_notion_sync_thread", lambda *a, **k: None
        )
        monkeypatch.setattr(app, "_install_shutdown_handlers", lambda _e: None)

    def test_exits_78_and_alerts_on_credential_refresh_error(
        self, monkeypatch
    ):
        monkeypatch.setattr(
            app,
            "get_credentials",
            MagicMock(side_effect=CredentialRefreshError("token dead")),
        )

        with pytest.raises(SystemExit) as exc_info:
            app.run()

        assert exc_info.value.code == EXIT_CREDENTIAL_FAILURE
        self.mock_notifier.send_message.assert_called_once()
        alert_args, alert_kwargs = self.mock_notifier.send_message.call_args
        alert_body = alert_args[0] if alert_args else alert_kwargs.get("text", "")
        assert "reauth" in alert_body
        assert "token dead" in alert_body
        # Poller path must not have been reached.
        self.mock_gmail_client.assert_not_called()

    def test_startup_alert_survives_telegram_failure(self, monkeypatch):
        """Telegram outage must not mask the credential failure — we
        still exit non-zero so the operator sees restart-crash-restart
        in docker/systemd logs and investigates."""
        from zashiki_warasi.notifications.telegram import TelegramError

        self.mock_notifier.send_message.side_effect = TelegramError("down")
        monkeypatch.setattr(
            app,
            "get_credentials",
            MagicMock(side_effect=CredentialRefreshError("token dead")),
        )

        with pytest.raises(SystemExit) as exc_info:
            app.run()

        assert exc_info.value.code == EXIT_CREDENTIAL_FAILURE


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
