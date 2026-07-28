"""Runtime configuration loaded from environment variables / .env files."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

DEFAULT_SCOPES: list[str] = [
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GMAIL_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    credentials_path: Path = Field(default=Path("credentials.json"))
    token_path: Path = Field(
        default=Path("~/.config/zashiki-warasi/token.json"),
    )
    scopes: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_SCOPES)
    )

    # Socket-level timeout (seconds) for every Gmail HTTP request.
    # Without this, httplib2's default of `None` lets the OS TCP
    # layer wait up to ~13 minutes (RTO doubling) before a dead
    # connection surfaces as ConnectionResetError — which stalls the
    # poller loop for the entire wait. 60s is well below the poll
    # cadence (30s tick, but individual requests should be sub-second).
    http_timeout_seconds: float = Field(default=60.0, gt=0)

    @field_validator("credentials_path", "token_path")
    @classmethod
    def _expand(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("scopes", mode="before")
    @classmethod
    def _split_scopes(cls, value: object) -> object:
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = Field(
        default="postgresql+psycopg://localhost/zashiki_warasi",
        alias="DATABASE_URL",
    )


class LLMSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LLM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    provider: Literal["llamacpp", "openai", "anthropic"] = "llamacpp"
    base_url: str = "http://localhost:8080/v1"
    api_key: str = "not-needed"
    model: str = "local-model"
    temperature: float = 0.2
    # Analyze produces a structured EmailAnalysis. Local models can
    # degenerate into a repeat loop inside a JSON string/array,
    # chewing through the whole context window (JAL Pay case:
    # completion=31023 on a 32k llama-server) before
    # finish_reason=length trips. Cap keeps that loop bounded so
    # LengthFinishReasonError fires fast enough that AnalysisFailed
    # takes over instead of blocking the poller.
    # Default 10922 ≈ 32768 / 3 — loose enough that a legitimate
    # long summary of a heavy newsletter (prompt ~5k, output several
    # hundred tokens) has plenty of headroom, tight enough that a
    # degenerate loop still aborts in ~10s instead of ~30s.
    analyze_max_tokens: int = Field(default=10922, gt=0)


class TelegramSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="TELEGRAM_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    bot_token: str = Field(
        default="",
        description="Bot token from BotFather (e.g. 1234567890:ABC...).",
    )
    chat_id: str = Field(
        default="",
        description=(
            "Destination chat: a numeric user/group id, a negative "
            "channel id, or '@channelusername'."
        ),
    )
    api_base: str = Field(
        default="https://api.telegram.org",
        description="Override only for testing or self-hosted bridges.",
    )
    timeout_seconds: float = 10.0


_VALID_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


class LoggingSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="LOG_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        # Normalize casing + whitespace at coercion time so validators
        # below only need to police membership — no per-field
        # .strip().upper() boilerplate.
        str_strip_whitespace=True,
        str_to_upper=True,
    )

    # Root logger level. Applied to every logger that hasn't been
    # given a specific override — including third-party ones (which
    # the bootstrap explicitly quiets to WARNING, see
    # zashiki_warasi.core.logging.configure_logging).
    level: str = Field(default="INFO")

    # Level for the `zashiki_warasi.*` tree specifically. Lets an
    # operator flip DEBUG for our code without unmuting httpx /
    # google.auth / openai chatter. None = inherit from root.
    # Field name matches env (`LOG_` prefix + `LEVEL_ZASHIKI` =
    # `LOG_LEVEL_ZASHIKI`) — no alias.
    level_zashiki: str | None = Field(default=None)

    @field_validator("level", mode="after")
    @classmethod
    def _check_root_level(cls, value: str) -> str:
        # Empty env vars (LOG_LEVEL="") already stripped to "" by
        # str_strip_whitespace — treat as "unset" and use default.
        if not value:
            return "INFO"
        _reject_unknown_level(value)
        return value

    @field_validator("level_zashiki", mode="after")
    @classmethod
    def _check_zashiki_level(cls, value: str | None) -> str | None:
        if not value:
            return None
        _reject_unknown_level(value)
        return value


def _reject_unknown_level(value: str) -> None:
    if value not in _VALID_LOG_LEVELS:
        raise ValueError(
            f"invalid log level {value!r}; must be one of "
            f"{sorted(_VALID_LOG_LEVELS)}"
        )


class NotionSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="NOTION_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    token: str = Field(
        default="",
        description="Internal integration token starting with 'secret_'.",
    )
    expense_database_id: str = Field(
        default="",
        description=(
            "UUID of the Notion database to write expenses into. "
            "The integration must be granted access to this database "
            "via its share menu in Notion."
        ),
    )
    timeout_seconds: float = 10.0

    sync_interval_seconds: int = Field(
        default=300,
        ge=0,
        description=(
            "Background Notion→DB sync interval. 0 disables the puller "
            "thread (the one-shot `sync-notion` subcommand still works)."
        ),
    )
