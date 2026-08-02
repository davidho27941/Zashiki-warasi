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
    EXIT_DB_UNREACHABLE,
    _build_checkpointer_pool,
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


class TestCheckpointerPoolLogging:
    """`run()` emits INFO lines symmetric on pool open/close so
    `grep 'checkpointer pool'` always shows both endpoints of a session
    (aids incident timelines). `_build_checkpointer_pool` emits DEBUG
    per new connection via the configure callback."""

    @pytest.fixture(autouse=True)
    def _stub_run_deps(self, monkeypatch):
        # Give `run()` a working notifier + creds + client + session
        # so it reaches the pool block. Poller is stubbed to return
        # immediately (empty tick) so we're not blocked on a real loop.
        monkeypatch.setattr(
            app, "TelegramNotifier", lambda: MagicMock()
        )
        monkeypatch.setattr(
            app, "get_session_factory", lambda: MagicMock()
        )
        monkeypatch.setattr(
            app, "get_credentials", MagicMock(return_value=MagicMock())
        )
        monkeypatch.setattr(app, "GmailClient", MagicMock())
        monkeypatch.setattr(app, "_build_notion", lambda: None)
        monkeypatch.setattr(
            app, "_start_notion_sync_thread", lambda *a, **k: None
        )
        monkeypatch.setattr(app, "_install_shutdown_handlers", lambda _e: None)

        # Fake pool + PostgresSaver as no-op context managers so `run()`
        # can enter/exit both without touching Postgres.
        self.fake_pool_class = MagicMock()
        self.fake_pool_instance = MagicMock()
        self.fake_pool_class.return_value = self.fake_pool_instance
        self.fake_pool_instance.__enter__.return_value = self.fake_pool_instance
        self.fake_pool_instance.__exit__.return_value = False
        monkeypatch.setattr(app, "ConnectionPool", self.fake_pool_class)

        # PostgresSaver(pool) is NOT a context manager — direct
        # construction is fine, only from_conn_string wraps it.
        fake_saver = MagicMock()
        monkeypatch.setattr(app, "PostgresSaver", lambda pool: fake_saver)

        # EmailAgent + Poller — the loop returns immediately.
        fake_agent = MagicMock()
        fake_agent.handle_email = MagicMock()
        monkeypatch.setattr(app, "EmailAgent", lambda **kw: fake_agent)

        fake_poller = MagicMock()
        fake_poller.run.return_value = None
        monkeypatch.setattr(app, "Poller", lambda **kw: fake_poller)

    def test_run_emits_info_on_pool_open_with_resolved_params(
        self, monkeypatch, caplog
    ):
        import logging as _logging

        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MIN_SIZE", "2")
        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MAX_SIZE", "8")
        monkeypatch.setenv(
            "DATABASE_CHECKPOINTER_POOL_MAX_LIFETIME_SECONDS", "900"
        )
        monkeypatch.setenv(
            "DATABASE_CHECKPOINTER_POOL_MAX_IDLE_SECONDS", "300"
        )

        with caplog.at_level(_logging.INFO, logger="zashiki_warasi.app"):
            app.run()

        open_records = [
            r for r in caplog.records
            if "checkpointer pool opened" in r.getMessage()
        ]
        assert len(open_records) == 1
        msg = open_records[0].getMessage()
        # All four resolved params surface so operators can verify env
        # overrides took effect without turning DEBUG on.
        assert "min=2" in msg
        assert "max=8" in msg
        assert "max_lifetime=900" in msg
        assert "max_idle=300" in msg

    def test_run_emits_info_on_pool_close(self, caplog):
        import logging as _logging

        with caplog.at_level(_logging.INFO, logger="zashiki_warasi.app"):
            app.run()

        close_records = [
            r for r in caplog.records
            if "checkpointer pool closed" in r.getMessage()
        ]
        assert len(close_records) == 1

    def test_configure_callback_logs_debug_with_application_name(
        self, monkeypatch, caplog
    ):
        """`configure` runs once per newly-created pool connection.
        Logs DEBUG naming the application_name so `pg_stat_activity`
        entries can be cross-referenced against our own log."""
        import logging as _logging

        from zashiki_warasi.app import (
            _CHECKPOINTER_APPLICATION_NAME,
            _build_checkpointer_pool,
        )
        from zashiki_warasi.core.config import DatabaseSettings

        # Capture the configure callback via the same fake-pool pattern.
        captured: dict = {}

        class _FakePool:
            def __init__(self, conninfo, **kwargs):
                captured.update(kwargs)

            def __enter__(self): return self
            def __exit__(self, *e): return False

        monkeypatch.setattr(app, "ConnectionPool", _FakePool)
        _build_checkpointer_pool(
            DatabaseSettings(),
            "postgresql://x",
            threading.Event(),
            threading.Event(),
        )
        configure = captured["configure"]

        # Fake connection whose `execute` succeeds silently — SET
        # application_name never actually reaches a real DB.
        fake_conn = MagicMock()

        with caplog.at_level(_logging.DEBUG, logger="zashiki_warasi.app"):
            configure(fake_conn)

        # `SET` doesn't accept `%s` params in Postgres (utility
        # statement) — we route through `set_config()` instead.
        fake_conn.execute.assert_called_once_with(
            "SELECT set_config('application_name', %s, false)",
            (_CHECKPOINTER_APPLICATION_NAME,),
        )
        # And we logged the fact.
        assert any(
            "checkpointer conn created" in r.getMessage()
            and _CHECKPOINTER_APPLICATION_NAME in r.getMessage()
            for r in caplog.records
        )


class TestRunWiresPollerHeartbeat:
    """`run()` reads `PollerSettings.heartbeat_interval_seconds` from
    env and passes it into `Poller(...)`. Pin this so the env override
    can't silently be dropped by a future refactor."""

    @pytest.fixture(autouse=True)
    def _stub_run_deps(self, monkeypatch):
        # Same run() stubs as TestCheckpointerPoolLogging above —
        # everything faked so we reach the Poller construction.
        monkeypatch.setattr(app, "TelegramNotifier", lambda: MagicMock())
        monkeypatch.setattr(app, "get_session_factory", lambda: MagicMock())
        monkeypatch.setattr(
            app, "get_credentials", MagicMock(return_value=MagicMock())
        )
        monkeypatch.setattr(app, "GmailClient", MagicMock())
        monkeypatch.setattr(app, "_build_notion", lambda: None)
        monkeypatch.setattr(
            app, "_start_notion_sync_thread", lambda *a, **k: None
        )
        monkeypatch.setattr(app, "_install_shutdown_handlers", lambda _e: None)

        fake_pool = MagicMock()
        fake_pool.__enter__.return_value = fake_pool
        fake_pool.__exit__.return_value = False
        monkeypatch.setattr(app, "ConnectionPool", lambda *a, **k: fake_pool)
        monkeypatch.setattr(app, "PostgresSaver", lambda pool: MagicMock())
        monkeypatch.setattr(app, "EmailAgent", lambda **kw: MagicMock())

        # Capture whatever kwargs `run()` passes into Poller(...) so we
        # can assert the heartbeat setting round-tripped.
        self.captured_kwargs: dict = {}

        def _fake_poller(**kw):
            self.captured_kwargs.update(kw)
            fake = MagicMock()
            fake.run.return_value = None
            return fake

        monkeypatch.setattr(app, "Poller", _fake_poller)

    def test_default_interval_reaches_poller(self, monkeypatch):
        monkeypatch.delenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", raising=False)
        app.run()
        assert self.captured_kwargs["heartbeat_interval_seconds"] == 1200

    def test_env_override_reaches_poller(self, monkeypatch):
        monkeypatch.setenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", "600")
        app.run()
        assert self.captured_kwargs["heartbeat_interval_seconds"] == 600

    def test_zero_reaches_poller_and_disables(self, monkeypatch):
        """`=0` is a valid config that disables the heartbeat — must
        propagate as-is to Poller (which knows to no-op on 0), not be
        rewritten to a "sensible" default along the way."""
        monkeypatch.setenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", "0")
        app.run()
        assert self.captured_kwargs["heartbeat_interval_seconds"] == 0


class TestBuildCheckpointerPool:
    """`_build_checkpointer_pool` constructs a psycopg-pool `ConnectionPool`
    parameterized from DatabaseSettings with LangGraph-required kwargs
    (autocommit, dict_row, prepare_threshold=0), and the three callbacks
    (check, configure, reconnect_failed) wired to our logging + shutdown
    plumbing. Tests spy on the ConnectionPool constructor so we don't
    need a real Postgres to run them."""

    def _capture_pool_kwargs(self, monkeypatch):
        captured: dict = {}

        class _FakePool:
            def __init__(self, conninfo, **kwargs):
                captured["conninfo"] = conninfo
                captured.update(kwargs)

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(app, "ConnectionPool", _FakePool)
        return captured

    def test_pool_sized_from_settings(self, monkeypatch):
        from zashiki_warasi.core.config import DatabaseSettings

        captured = self._capture_pool_kwargs(monkeypatch)
        settings = DatabaseSettings(
            checkpointer_pool_min_size=2,
            checkpointer_pool_max_size=7,
            checkpointer_pool_max_lifetime_seconds=900,
            checkpointer_pool_max_idle_seconds=300,
        )
        _build_checkpointer_pool(
            settings, "postgresql://x", threading.Event(), threading.Event()
        )
        assert captured["min_size"] == 2
        assert captured["max_size"] == 7
        assert captured["max_lifetime"] == 900
        assert captured["max_idle"] == 300

    def test_pool_receives_langgraph_required_conn_kwargs(self, monkeypatch):
        """PostgresSaver requires autocommit + dict_row row_factory +
        prepare_threshold=0 on every connection. Wired via `kwargs`
        so freshly-provisioned pool connections come pre-configured."""
        from psycopg.rows import dict_row
        from zashiki_warasi.core.config import DatabaseSettings

        captured = self._capture_pool_kwargs(monkeypatch)
        _build_checkpointer_pool(
            DatabaseSettings(),
            "postgresql://x",
            threading.Event(),
            threading.Event(),
        )
        assert captured["kwargs"]["autocommit"] is True
        assert captured["kwargs"]["prepare_threshold"] == 0
        assert captured["kwargs"]["row_factory"] is dict_row

    def test_pool_opens_lazily(self, monkeypatch):
        """`open=False` — caller owns the `with pool:` block. If pool
        opened eagerly we'd race with the caller's context-manager
        exit ordering (checkpointer must flush before pool closes)."""
        from zashiki_warasi.core.config import DatabaseSettings

        captured = self._capture_pool_kwargs(monkeypatch)
        _build_checkpointer_pool(
            DatabaseSettings(),
            "postgresql://x",
            threading.Event(),
            threading.Event(),
        )
        assert captured["open"] is False

    def test_check_callback_reraises_and_logs_warning(self, monkeypatch, caplog):
        import logging

        from zashiki_warasi.core.config import DatabaseSettings

        captured = self._capture_pool_kwargs(monkeypatch)
        _build_checkpointer_pool(
            DatabaseSettings(),
            "postgresql://x",
            threading.Event(),
            threading.Event(),
        )
        check = captured["check"]

        # Stub the underlying builtin so we can force a "stale" outcome
        # without a live Postgres.
        stub_check = MagicMock(side_effect=RuntimeError("dead conn"))
        monkeypatch.setattr(
            "psycopg_pool.ConnectionPool.check_connection", stub_check
        )

        with caplog.at_level(logging.WARNING, logger="zashiki_warasi.app"):
            with pytest.raises(RuntimeError, match="dead conn"):
                check(MagicMock())

        # Load-bearing: pool's discard path is triggered by the raised
        # exception — swallowing would leave the dead conn in circulation.
        stub_check.assert_called_once()
        assert any(
            "discarded stale connection" in r.getMessage()
            for r in caplog.records
        )

    def test_reconnect_failed_callback_sets_flag_and_logs_critical(
        self, monkeypatch, caplog
    ):
        import logging

        from zashiki_warasi.core.config import DatabaseSettings

        captured = self._capture_pool_kwargs(monkeypatch)
        stop_event = threading.Event()
        unreachable = threading.Event()
        _build_checkpointer_pool(
            DatabaseSettings(), "postgresql://x", stop_event, unreachable
        )
        reconnect_failed = captured["reconnect_failed"]

        fake_pool = MagicMock()
        fake_pool.reconnect_timeout = 300

        with caplog.at_level(logging.CRITICAL, logger="zashiki_warasi.app"):
            reconnect_failed(fake_pool)

        assert unreachable.is_set()
        assert stop_event.is_set()
        critical_msgs = [
            r.getMessage()
            for r in caplog.records
            if r.levelno == logging.CRITICAL
        ]
        assert any(
            "reconnect failed" in m and "300s" in m for m in critical_msgs
        )


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
