"""Settings loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from zashiki_warasi.core.config import (
    DEFAULT_SCOPES,
    DatabaseSettings,
    GmailSettings,
    LLMSettings,
    LoggingSettings,
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
