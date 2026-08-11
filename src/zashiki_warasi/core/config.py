"""Runtime configuration loaded from environment variables / .env files."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, field_validator, model_validator
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
        env_prefix="DATABASE_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # `DATABASE_URL` is the de-facto Twelve-Factor / Heroku env name;
    # keeping the alias so operators don't have to relearn it just
    # because we added a prefix for the new pool fields below.
    database_url: str = Field(
        default="postgresql+psycopg://localhost/zashiki_warasi",
        alias="DATABASE_URL",
    )

    # LangGraph checkpointer connection pool tunables. Env names match
    # field names (prefix `DATABASE_` + name) — no aliases. Defaults are
    # sized for a single-threaded homelab poller; raise max_size for a
    # chattier deployment. All four enforce `gt=0` (a zero would either
    # starve the pool or silently defeat recycling).
    checkpointer_pool_min_size: int = Field(default=1, gt=0)
    checkpointer_pool_max_size: int = Field(default=5, gt=0)
    checkpointer_pool_max_lifetime_seconds: float = Field(
        default=1800.0, gt=0
    )
    checkpointer_pool_max_idle_seconds: float = Field(
        default=600.0, gt=0
    )

    @field_validator("checkpointer_pool_max_size")
    @classmethod
    def _check_pool_size_ordering(cls, value: int, info) -> int:
        # A misconfigured `max < min` would deadlock the pool at open()
        # because it can't provision `min_size` connections without
        # exceeding `max_size`. Fail fast at startup.
        min_size = info.data.get("checkpointer_pool_min_size")
        if min_size is not None and value < min_size:
            raise ValueError(
                f"checkpointer_pool_max_size ({value}) must be >= "
                f"checkpointer_pool_min_size ({min_size})"
            )
        return value


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


class PollerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="POLLER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )
    # Intentionally empty in v1.0. The v0.x fields
    # (heartbeat_interval_seconds, interval_seconds) have moved out of
    # the process: cadence is owned by the external scheduler (cron /
    # k8s CronJob) and liveness by /healthz + the scheduler's own logs.
    # The class is retained so future poller-scoped settings have an
    # obvious home without changing imports.


class HttpSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="HTTP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Default `127.0.0.1` is a safe fail-closed for local dev — the
    # container's compose/Helm defaults override to `0.0.0.0` so the
    # in-cluster/in-network scheduler can reach /poll. If the operator
    # ever sets bind_host to something reachable beyond loopback, the
    # cross-field validator below requires an api_key so /poll and
    # /reauth aren't exposed unauthenticated.
    bind_host: str = "127.0.0.1"
    bind_port: int = Field(default=8080, gt=0, lt=65536)
    # Optional shared-secret. When unset (None or empty string), /poll
    # and /reauth accept requests without an X-API-Key header. Setting
    # any non-empty value requires the header to match.
    api_key: str | None = None

    @model_validator(mode="after")
    def _require_api_key_on_public_bind(self) -> "HttpSettings":
        # Fail-fast at settings-load time: if the operator opens the
        # bind beyond loopback but forgets to set a key, /poll and
        # /reauth would be reachable from anywhere on the interface
        # with no auth. Refuse to boot instead of quietly exposing.
        if self.bind_host != "127.0.0.1" and not self.api_key:
            raise ValueError(
                f"HTTP_API_KEY must be set when HTTP_BIND_HOST is "
                f"non-loopback (got HTTP_BIND_HOST={self.bind_host!r}). "
                "Set HTTP_API_KEY to a shared secret, or revert "
                "HTTP_BIND_HOST to 127.0.0.1 for loopback-only access."
            )
        return self


class OAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OAUTH_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Absolute URL Google will redirect the operator's browser to after
    # the consent screen. MUST match a redirect URI registered in the
    # Google Cloud Console for this OAuth client. Loopback URIs like
    # `http://127.0.0.1:8080/auth/callback` are allowed without HTTPS
    # (Google special-cases them); non-loopback hosts require HTTPS +
    # a public CA cert. Left None here so tools that don't use the web
    # flow (CLI reauth, /healthz) don't require it; the endpoints that
    # do need it raise HTTP 500 when it's missing at call time.
    redirect_uri: str | None = None


# v0.x env vars kept alive as soft-migration signals. Presence at
# startup logs INFO ("set but ignored") so operators upgrading from
# v0.6.x see the removed vars in their old .env and know to clean up.
# Adding a var here (with the reason) is the entire "removal" contract
# — no fail-fast, no crash, just a visible line in the boot log.
_REMOVED_ENV_VARS: tuple[tuple[str, str], ...] = (
    (
        "POLLER_HEARTBEAT_INTERVAL_SECONDS",
        "removed in v1.0 — heartbeat superseded by /healthz + cron logs",
    ),
    (
        "POLLER_INTERVAL_SECONDS",
        "removed in v1.0 — cadence owned by external scheduler (cron / k8s CronJob)",
    ),
)


def warn_removed_env_vars(logger: logging.Logger | None = None) -> None:
    """Emit one INFO per removed-but-present env var. Idempotent."""
    log = logger or logging.getLogger(__name__)
    for var, reason in _REMOVED_ENV_VARS:
        if os.environ.get(var):
            log.info(f"env var {var} is set but ignored ({reason})")


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
