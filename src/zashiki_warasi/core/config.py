"""Runtime configuration loaded from environment variables / .env files."""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
    scopes: list[str] = Field(default_factory=lambda: list(DEFAULT_SCOPES))

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
