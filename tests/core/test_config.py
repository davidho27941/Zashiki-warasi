"""Settings loading and validation."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from zashiki_warasi.core.config import (
    DEFAULT_SCOPES,
    DatabaseSettings,
    GmailSettings,
    HttpSettings,
    LLMSettings,
    LoggingSettings,
    OAuthSettings,
    PollerSettings,
    warn_removed_env_vars,
)


# --- GmailSettings ---


class TestGmailSettings:
    def test_defaults(self, monkeypatch, tmp_path):
        # Isolate from any real .env / env vars
        for var in (
            "GMAIL_CREDENTIALS_PATH",
            "GMAIL_TOKEN_PATH",
            "GMAIL_SCOPES",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.chdir(tmp_path)

        s = GmailSettings()
        assert s.credentials_path == Path("credentials.json")
        # ~ should be expanded in defaults
        assert "~" not in str(s.token_path)
        assert s.token_path.is_absolute()
        assert list(s.scopes) == list(DEFAULT_SCOPES)

    def test_credentials_path_expands_tilde(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", "~/creds.json")
        s = GmailSettings()
        assert "~" not in str(s.credentials_path)
        assert s.credentials_path.is_absolute()

    def test_token_path_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        target = tmp_path / "token.json"
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(target))
        s = GmailSettings()
        assert s.token_path == target

    def test_scopes_comma_separated_from_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            "GMAIL_SCOPES",
            "https://www.googleapis.com/auth/gmail.readonly, "
            "https://www.googleapis.com/auth/gmail.modify",
        )
        s = GmailSettings()
        assert s.scopes == [
            "https://www.googleapis.com/auth/gmail.readonly",
            "https://www.googleapis.com/auth/gmail.modify",
        ]

    def test_default_scopes_not_shared_between_instances(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.chdir(tmp_path)
        a = GmailSettings()
        b = GmailSettings()
        # Pydantic frozen-style models still own distinct lists via default_factory
        assert a.scopes is not b.scopes


# --- DatabaseSettings ---


class TestDatabaseSettings:
    def test_default(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("DATABASE_URL", raising=False)
        s = DatabaseSettings()
        assert s.database_url.startswith("postgresql+psycopg://")

    def test_database_url_alias(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv(
            "DATABASE_URL",
            "postgresql+psycopg://user:pw@db.example/zw",
        )
        s = DatabaseSettings()
        assert s.database_url == "postgresql+psycopg://user:pw@db.example/zw"

    # --- checkpointer pool tunables ---

    def _clean_env(self, monkeypatch):
        for var in (
            "DATABASE_CHECKPOINTER_POOL_MIN_SIZE",
            "DATABASE_CHECKPOINTER_POOL_MAX_SIZE",
            "DATABASE_CHECKPOINTER_POOL_MAX_LIFETIME_SECONDS",
            "DATABASE_CHECKPOINTER_POOL_MAX_IDLE_SECONDS",
        ):
            monkeypatch.delenv(var, raising=False)

    def test_pool_defaults(self, monkeypatch, tmp_path):
        """Sized for a single-threaded homelab poller. max_idle=600 sits
        below typical NAT eviction windows (5-15 min); max_lifetime=1800
        matches SQLAlchemy engine's pool_recycle for consistency."""
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        s = DatabaseSettings()
        assert s.checkpointer_pool_min_size == 1
        assert s.checkpointer_pool_max_size == 5
        assert s.checkpointer_pool_max_lifetime_seconds == 1800.0
        assert s.checkpointer_pool_max_idle_seconds == 600.0

    def test_pool_min_size_env_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MIN_SIZE", "3")
        s = DatabaseSettings()
        assert s.checkpointer_pool_min_size == 3

    def test_pool_max_size_env_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MAX_SIZE", "10")
        s = DatabaseSettings()
        assert s.checkpointer_pool_max_size == 10

    def test_pool_max_lifetime_env_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv(
            "DATABASE_CHECKPOINTER_POOL_MAX_LIFETIME_SECONDS", "3600"
        )
        s = DatabaseSettings()
        assert s.checkpointer_pool_max_lifetime_seconds == 3600.0

    def test_pool_max_idle_env_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv(
            "DATABASE_CHECKPOINTER_POOL_MAX_IDLE_SECONDS", "300"
        )
        s = DatabaseSettings()
        assert s.checkpointer_pool_max_idle_seconds == 300.0

    @pytest.mark.parametrize(
        "field",
        [
            "DATABASE_CHECKPOINTER_POOL_MIN_SIZE",
            "DATABASE_CHECKPOINTER_POOL_MAX_SIZE",
            "DATABASE_CHECKPOINTER_POOL_MAX_LIFETIME_SECONDS",
            "DATABASE_CHECKPOINTER_POOL_MAX_IDLE_SECONDS",
        ],
    )
    def test_pool_field_rejects_zero(self, monkeypatch, tmp_path, field):
        """gt=0 constraint — a zero would either starve the pool
        (min_size=0 + max_size=0 → nothing to hand out) or silently
        defeat recycling (max_lifetime=0)."""
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv(field, "0")
        with pytest.raises(Exception):
            DatabaseSettings()

    def test_pool_max_smaller_than_min_rejected(self, monkeypatch, tmp_path):
        """max < min would deadlock the pool at open() — can't
        provision `min_size` connections without exceeding `max_size`."""
        monkeypatch.chdir(tmp_path)
        self._clean_env(monkeypatch)
        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MIN_SIZE", "5")
        monkeypatch.setenv("DATABASE_CHECKPOINTER_POOL_MAX_SIZE", "1")
        with pytest.raises(Exception, match="must be >="):
            DatabaseSettings()


# --- LLMSettings ---


class TestLLMSettings:
    def test_defaults_target_local_llamacpp(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in (
            "LLM_PROVIDER",
            "LLM_BASE_URL",
            "LLM_API_KEY",
            "LLM_MODEL",
            "LLM_TEMPERATURE",
            "LLM_ANALYZE_MAX_TOKENS",
        ):
            monkeypatch.delenv(var, raising=False)
        s = LLMSettings()
        assert s.provider == "llamacpp"
        assert s.base_url == "http://localhost:8080/v1"
        assert s.model == "local-model"
        assert s.temperature == pytest.approx(0.2)
        assert s.analyze_max_tokens == 10922

    def test_provider_literal_rejects_unknown(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "groq")
        with pytest.raises(Exception):
            LLMSettings()

    def test_provider_openai_accepted(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_PROVIDER", "openai")
        monkeypatch.setenv("LLM_API_KEY", "sk-test")
        monkeypatch.setenv("LLM_MODEL", "gpt-4")
        s = LLMSettings()
        assert s.provider == "openai"
        assert s.api_key == "sk-test"
        assert s.model == "gpt-4"

    def test_temperature_coerced_from_string(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_TEMPERATURE", "0.9")
        s = LLMSettings()
        assert s.temperature == pytest.approx(0.9)

    def test_analyze_max_tokens_env_override(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_ANALYZE_MAX_TOKENS", "512")
        s = LLMSettings()
        assert s.analyze_max_tokens == 512

    def test_analyze_max_tokens_rejects_non_positive(self, monkeypatch, tmp_path):
        """gt=0 constraint — 0 or negative caps make no sense (a
        cap of 0 would truncate everything, silently masking real
        failures as LengthFinishReasonError)."""
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LLM_ANALYZE_MAX_TOKENS", "0")
        with pytest.raises(Exception):
            LLMSettings()


# --- LoggingSettings ---


class TestLoggingSettings:
    """Two-knob log-level config: LOG_LEVEL for root, LOG_LEVEL_ZASHIKI
    for our own logger tree so operators can flip DEBUG on our code
    without unmuting httpx / google.auth / openai chatter."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in ("LOG_LEVEL", "LOG_LEVEL_ZASHIKI"):
            monkeypatch.delenv(var, raising=False)

    def test_defaults_are_info_root_and_none_for_zashiki(self):
        s = LoggingSettings()
        assert s.level == "INFO"
        assert s.level_zashiki is None

    def test_root_level_env_override(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "WARNING")
        s = LoggingSettings()
        assert s.level == "WARNING"

    def test_level_zashiki_env_override(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL_ZASHIKI", "DEBUG")
        s = LoggingSettings()
        assert s.level_zashiki == "DEBUG"

    def test_level_is_case_insensitive(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        s = LoggingSettings()
        assert s.level == "DEBUG"  # normalized to canonical upper form

    def test_level_strips_whitespace(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "  info  ")
        s = LoggingSettings()
        assert s.level == "INFO"

    def test_invalid_root_level_raises(self, monkeypatch):
        """Fail-fast on typos — silently falling back to INFO would
        make the operator think DEBUG was on when it wasn't."""
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        with pytest.raises(Exception, match="invalid log level"):
            LoggingSettings()

    def test_invalid_level_zashiki_raises(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL_ZASHIKI", "loud")
        with pytest.raises(Exception, match="invalid log level"):
            LoggingSettings()

    def test_empty_level_zashiki_is_none(self, monkeypatch):
        """Empty string reads as unset — inherit from root, don't
        silently coerce to some default that would surprise operators."""
        monkeypatch.setenv("LOG_LEVEL_ZASHIKI", "")
        s = LoggingSettings()
        assert s.level_zashiki is None

    def test_empty_root_level_falls_back_to_info(self, monkeypatch):
        monkeypatch.setenv("LOG_LEVEL", "")
        s = LoggingSettings()
        assert s.level == "INFO"

    def test_all_five_canonical_levels_accepted(self, monkeypatch):
        for level in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            monkeypatch.setenv("LOG_LEVEL", level)
            assert LoggingSettings().level == level


# --- PollerSettings ---


class TestPollerSettings:
    """v1.0 has no poller-scoped settings — cadence is external cron,
    liveness is /healthz. The class is retained as an empty namespace
    for future fields; test that instantiation works and that removed
    env vars are ignored (not fail-fast)."""

    def test_instantiation_is_a_noop(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        # v0.x env vars must not crash v1.0 startup — pydantic-settings
        # `extra="ignore"` handles this. Regression against a future
        # change flipping to `extra="forbid"` and breaking upgraders.
        monkeypatch.setenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", "1200")
        monkeypatch.setenv("POLLER_INTERVAL_SECONDS", "30")
        PollerSettings()  # no exception


# --- HttpSettings ---


class TestHttpSettings:
    """FastAPI listener config. Loopback default is fail-closed; the
    cross-field validator refuses a non-loopback bind without an API
    key so unauthed /poll / /reauth aren't silently exposed."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        for var in ("HTTP_BIND_HOST", "HTTP_BIND_PORT", "HTTP_API_KEY"):
            monkeypatch.delenv(var, raising=False)

    def test_defaults(self):
        s = HttpSettings()
        assert s.bind_host == "127.0.0.1"
        assert s.bind_port == 8080
        assert s.api_key is None

    def test_bind_host_env_override_loopback(self, monkeypatch):
        # 127.0.0.1 override is still loopback → no api_key required.
        monkeypatch.setenv("HTTP_BIND_HOST", "127.0.0.1")
        s = HttpSettings()
        assert s.bind_host == "127.0.0.1"

    def test_bind_port_env_override(self, monkeypatch):
        monkeypatch.setenv("HTTP_BIND_PORT", "9000")
        s = HttpSettings()
        assert s.bind_port == 9000

    def test_api_key_env_override(self, monkeypatch):
        monkeypatch.setenv("HTTP_API_KEY", "shhh")
        s = HttpSettings()
        assert s.api_key == "shhh"

    @pytest.mark.parametrize("port", ["0", "65536", "-1"])
    def test_port_out_of_range_rejected(self, monkeypatch, port):
        monkeypatch.setenv("HTTP_BIND_PORT", port)
        with pytest.raises(Exception):
            HttpSettings()

    def test_public_bind_without_api_key_rejected(self, monkeypatch):
        """Fail-fast guard: opening bind beyond loopback without an
        API key would expose unauthenticated /poll + /reauth. Refuse
        to load rather than silently boot into that state."""
        monkeypatch.setenv("HTTP_BIND_HOST", "0.0.0.0")
        with pytest.raises(Exception, match="HTTP_API_KEY"):
            HttpSettings()

    def test_public_bind_with_api_key_accepted(self, monkeypatch):
        monkeypatch.setenv("HTTP_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("HTTP_API_KEY", "shhh")
        s = HttpSettings()
        assert s.bind_host == "0.0.0.0"
        assert s.api_key == "shhh"

    def test_public_bind_with_empty_api_key_rejected(self, monkeypatch):
        # Empty string is truthy-string but semantically unset — the
        # validator treats "" as absent (same as None).
        monkeypatch.setenv("HTTP_BIND_HOST", "0.0.0.0")
        monkeypatch.setenv("HTTP_API_KEY", "")
        with pytest.raises(Exception, match="HTTP_API_KEY"):
            HttpSettings()


# --- OAuthSettings ---


class TestOAuthSettings:
    """redirect_uri is optional at settings load — CLI reauth and
    /healthz don't need it. Endpoints that do (POST /reauth,
    GET /auth/start) enforce it at call time with a clear 500 error."""

    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("OAUTH_REDIRECT_URI", raising=False)

    def test_default_is_none(self):
        s = OAuthSettings()
        assert s.redirect_uri is None

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "OAUTH_REDIRECT_URI", "http://127.0.0.1:8080/auth/callback"
        )
        s = OAuthSettings()
        assert s.redirect_uri == "http://127.0.0.1:8080/auth/callback"


# --- removed env var soft-migration ---


class TestWarnRemovedEnvVars:
    """v0.x env vars that are gone in v1.0 must not silently vanish
    from the operator's view — a single INFO per set-but-ignored var
    at startup is the entire deprecation contract."""

    def test_no_removed_vars_present_is_silent(self, monkeypatch, caplog):
        monkeypatch.delenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", raising=False)
        monkeypatch.delenv("POLLER_INTERVAL_SECONDS", raising=False)
        with caplog.at_level(logging.INFO):
            warn_removed_env_vars()
        assert not any(
            "set but ignored" in r.message for r in caplog.records
        )

    def test_present_var_logs_info_with_reason(self, monkeypatch, caplog):
        monkeypatch.setenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", "1200")
        with caplog.at_level(logging.INFO):
            warn_removed_env_vars()
        matches = [r for r in caplog.records if "set but ignored" in r.message]
        assert len(matches) == 1
        assert "POLLER_HEARTBEAT_INTERVAL_SECONDS" in matches[0].message
        assert "heartbeat" in matches[0].message.lower()

    def test_both_vars_present_logs_both(self, monkeypatch, caplog):
        monkeypatch.setenv("POLLER_HEARTBEAT_INTERVAL_SECONDS", "1200")
        monkeypatch.setenv("POLLER_INTERVAL_SECONDS", "30")
        with caplog.at_level(logging.INFO):
            warn_removed_env_vars()
        matches = [r for r in caplog.records if "set but ignored" in r.message]
        assert len(matches) == 2


# --- LoggingSettings.format (LOG_FORMAT) ---


class TestLoggingSettingsFormat:
    def test_default_is_text(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv("LOG_FORMAT", raising=False)
        assert LoggingSettings().format == "text"

    def test_env_json_lowercased(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_FORMAT", "json")
        assert LoggingSettings().format == "json"

    def test_env_uppercase_still_accepted(self, monkeypatch, tmp_path):
        # str_to_upper=True on the class uppercases the value at
        # coercion; the validator must lower it before comparing.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_FORMAT", "JSON")
        assert LoggingSettings().format == "json"

    def test_invalid_raises(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_FORMAT", "yaml")
        with pytest.raises(Exception) as exc_info:
            LoggingSettings()
        assert "yaml" in str(exc_info.value).lower()

    def test_empty_string_falls_back_to_default(self, monkeypatch, tmp_path):
        # str_strip_whitespace + empty string -> ""; validator should
        # treat that as "unset" and use the default, mirroring the
        # existing level validator behavior.
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("LOG_FORMAT", "")
        assert LoggingSettings().format == "text"


# --- ObservabilitySettings ---


class TestObservabilitySettings:
    @pytest.fixture(autouse=True)
    def _isolate_env(self, monkeypatch, tmp_path):
        # ObservabilitySettings uses an empty env_prefix — it will pick
        # up ANY env var whose name matches its field names. Isolate by
        # clearing the OTEL_* namespace + chdir'ing out of the repo so
        # its .env doesn't leak.
        for var in (
            "OTEL_ENABLED",
            "OTEL_SERVICE_NAME",
            "OTEL_EXPORTER_OTLP_ENDPOINT",
            "OTEL_EXPORTER_OTLP_PROTOCOL",
            "OTEL_TRACES_SAMPLER",
            "OTEL_TRACES_SAMPLER_ARG",
            "OTEL_RESOURCE_ATTRIBUTES",
        ):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.chdir(tmp_path)

    def test_defaults(self):
        from zashiki_warasi.core.config import ObservabilitySettings

        s = ObservabilitySettings()
        assert s.otel_enabled is False
        assert s.otel_service_name == "zashiki-warasi"
        assert s.otel_exporter_otlp_endpoint == "http://localhost:4317"
        assert s.otel_exporter_otlp_protocol == "grpc"
        assert s.otel_traces_sampler == "parentbased_traceidratio"
        assert s.otel_traces_sampler_arg == 1.0
        assert s.otel_resource_attributes == ""

    def test_enabled_toggle(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv("OTEL_ENABLED", "1")
        assert ObservabilitySettings().otel_enabled is True

    def test_service_name_override(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv("OTEL_SERVICE_NAME", "zw-prod")
        assert ObservabilitySettings().otel_service_name == "zw-prod"

    def test_endpoint_override(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv(
            "OTEL_EXPORTER_OTLP_ENDPOINT", "http://otelcol:4317"
        )
        assert (
            ObservabilitySettings().otel_exporter_otlp_endpoint
            == "http://otelcol:4317"
        )

    def test_resource_attributes_comma_separated(self, monkeypatch):
        # Regression pin for the pydantic-settings NoDecode gotcha:
        # this field must stay `str`-typed so the OTel-standard comma
        # form isn't JSON-decoded by EnvSettingsSource. Value is
        # forwarded to the OTel SDK's Resource merger as-is.
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv(
            "OTEL_RESOURCE_ATTRIBUTES",
            "deployment.environment=prod,team=obs",
        )
        assert (
            ObservabilitySettings().otel_resource_attributes
            == "deployment.environment=prod,team=obs"
        )

    def test_sampler_arg_valid_range(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        for value in ("0.0", "0.1", "0.5", "1.0"):
            monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", value)
            assert ObservabilitySettings().otel_traces_sampler_arg == float(
                value
            )

    def test_sampler_arg_above_1_rejected(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "1.5")
        with pytest.raises(Exception) as exc_info:
            ObservabilitySettings()
        assert "1.5" in str(exc_info.value) or "1.0" in str(exc_info.value)

    def test_sampler_arg_negative_rejected(self, monkeypatch):
        from zashiki_warasi.core.config import ObservabilitySettings

        monkeypatch.setenv("OTEL_TRACES_SAMPLER_ARG", "-0.1")
        with pytest.raises(Exception):
            ObservabilitySettings()

    def test_no_cross_contamination_with_logging(self, monkeypatch):
        # Regression pin: LOG_FORMAT belongs to LoggingSettings (prefix
        # LOG_); OTEL_* belongs to ObservabilitySettings (empty prefix).
        # Setting one MUST NOT affect the other model's population.
        from zashiki_warasi.core.config import (
            LoggingSettings,
            ObservabilitySettings,
        )

        monkeypatch.setenv("LOG_FORMAT", "json")
        monkeypatch.setenv("OTEL_SERVICE_NAME", "zw-x")

        log_settings = LoggingSettings()
        obs_settings = ObservabilitySettings()

        # LoggingSettings picks up LOG_FORMAT, not OTEL_SERVICE_NAME
        assert log_settings.format == "json"
        # ObservabilitySettings picks up OTEL_SERVICE_NAME, defaults for
        # LOG_FORMAT (which it doesn't own)
        assert obs_settings.otel_service_name == "zw-x"
        # Neither leaks into the other's un-owned fields
        assert not hasattr(log_settings, "otel_service_name")
        assert not hasattr(obs_settings, "format")
