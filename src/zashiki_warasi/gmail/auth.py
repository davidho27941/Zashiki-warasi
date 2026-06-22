"""OAuth 2.0 Installed App flow for personal Gmail accounts."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Sequence

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

from zashiki_warasi.core.config import GmailSettings


def get_credentials(settings: GmailSettings | None = None) -> Credentials:
    settings = settings or GmailSettings()
    creds = _load_cached(settings.token_path, settings.scopes)

    if creds and creds.valid:
        return creds

    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        creds = _run_installed_flow(settings.credentials_path, settings.scopes)

    _persist(creds, settings.token_path)
    return creds


def _load_cached(token_path: Path, scopes: Sequence[str]) -> Credentials | None:
    if not token_path.exists():
        return None
    return Credentials.from_authorized_user_file(str(token_path), list(scopes))


def _run_installed_flow(
    credentials_path: Path, scopes: Sequence[str]
) -> Credentials:
    if not credentials_path.exists():
        raise FileNotFoundError(
            f"OAuth client secrets not found at {credentials_path}. "
            "Download it from Google Cloud Console (OAuth 2.0 Client IDs, "
            "type: Desktop app) and set GMAIL_CREDENTIALS_PATH."
        )
    flow = InstalledAppFlow.from_client_secrets_file(
        str(credentials_path), list(scopes)
    )
    return flow.run_local_server(port=0)


def _persist(creds: Credentials, token_path: Path) -> None:
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    os.chmod(token_path, 0o600)
