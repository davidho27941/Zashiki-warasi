"""OAuth 2.0 Installed App flow: cache, refresh, interactive, persist."""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from zashiki_warasi.core.config import GmailSettings
from zashiki_warasi.gmail import auth


def _settings(tmp_path: Path, *, with_credentials: bool = True) -> GmailSettings:
    credentials_path = tmp_path / "credentials.json"
    if with_credentials:
        credentials_path.write_text('{"installed": {"client_id": "x"}}')
    return GmailSettings(
        credentials_path=credentials_path,
        token_path=tmp_path / "subdir" / "token.json",
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
    )


def _fake_creds(
    *, valid: bool = True, expired: bool = False, refresh_token: str | None = None
) -> MagicMock:
    creds = MagicMock()
    creds.valid = valid
    creds.expired = expired
    creds.refresh_token = refresh_token
    creds.to_json.return_value = '{"token": "abc"}'
    return creds


# --- cached + valid: short-circuit ---


class TestValidCachedToken:
    def test_returns_cached_credentials_without_refresh_or_flow(
        self, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        settings.token_path.write_text("{}")

        cached = _fake_creds(valid=True)
        load_mock = MagicMock(return_value=cached)
        flow_mock = MagicMock()
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.Credentials.from_authorized_user_file",
            load_mock,
        )
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.InstalledAppFlow.from_client_secrets_file",
            flow_mock,
        )

        result = auth.get_credentials(settings)

        assert result is cached
        load_mock.assert_called_once()
        flow_mock.assert_not_called()
        cached.refresh.assert_not_called()


# --- cached + expired + refresh_token: refresh path ---


class TestRefreshPath:
    def test_refreshes_when_expired_and_refresh_token_present(
        self, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        settings.token_path.write_text("{}")

        cached = _fake_creds(valid=False, expired=True, refresh_token="rt")
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.Credentials.from_authorized_user_file",
            MagicMock(return_value=cached),
        )
        flow_mock = MagicMock()
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.InstalledAppFlow.from_client_secrets_file",
            flow_mock,
        )

        result = auth.get_credentials(settings)

        cached.refresh.assert_called_once()
        flow_mock.assert_not_called()
        assert result is cached
        # Persisted to disk after refresh
        assert settings.token_path.exists()


# --- no cache or expired-without-refresh-token: installed flow ---


class TestInstalledFlow:
    def test_runs_installed_flow_when_no_cached_token(
        self, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        # token_path does not exist

        flow_instance = MagicMock()
        new_creds = _fake_creds(valid=True)
        flow_instance.run_local_server.return_value = new_creds
        flow_factory = MagicMock(return_value=flow_instance)
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.InstalledAppFlow.from_client_secrets_file",
            flow_factory,
        )

        result = auth.get_credentials(settings)

        flow_factory.assert_called_once_with(
            str(settings.credentials_path), list(settings.scopes)
        )
        flow_instance.run_local_server.assert_called_once_with(port=0)
        assert result is new_creds

    def test_runs_installed_flow_when_cached_expired_without_refresh_token(
        self, tmp_path, monkeypatch
    ):
        settings = _settings(tmp_path)
        settings.token_path.parent.mkdir(parents=True, exist_ok=True)
        settings.token_path.write_text("{}")

        cached = _fake_creds(valid=False, expired=True, refresh_token=None)
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.Credentials.from_authorized_user_file",
            MagicMock(return_value=cached),
        )
        new_creds = _fake_creds(valid=True)
        flow_instance = MagicMock()
        flow_instance.run_local_server.return_value = new_creds
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.InstalledAppFlow.from_client_secrets_file",
            MagicMock(return_value=flow_instance),
        )

        result = auth.get_credentials(settings)

        cached.refresh.assert_not_called()
        flow_instance.run_local_server.assert_called_once()
        assert result is new_creds


# --- missing credentials.json: friendly error ---


class TestMissingCredentialsFile:
    def test_raises_filenotfound_with_actionable_message(self, tmp_path):
        settings = _settings(tmp_path, with_credentials=False)

        with pytest.raises(FileNotFoundError) as exc_info:
            auth.get_credentials(settings)

        msg = str(exc_info.value)
        assert str(settings.credentials_path) in msg
        assert "Google Cloud Console" in msg
        assert "GMAIL_CREDENTIALS_PATH" in msg


# --- _persist behaviour ---


class TestPersist:
    def test_creates_parent_directories(self, tmp_path):
        nested = tmp_path / "a" / "b" / "c" / "token.json"
        creds = _fake_creds(valid=True)
        creds.to_json.return_value = '{"tok": 1}'

        auth._persist(creds, nested)

        assert nested.exists()
        assert nested.read_text() == '{"tok": 1}'

    def test_chmods_token_file_to_0600(self, tmp_path):
        token_path = tmp_path / "token.json"
        creds = _fake_creds(valid=True)
        creds.to_json.return_value = "{}"

        auth._persist(creds, token_path)

        mode = stat.S_IMODE(token_path.stat().st_mode)
        assert mode == 0o600

    def test_overwrites_existing_token(self, tmp_path):
        token_path = tmp_path / "token.json"
        token_path.write_text("old")
        creds = _fake_creds(valid=True)
        creds.to_json.return_value = "new"

        auth._persist(creds, token_path)

        assert token_path.read_text() == "new"


# --- _load_cached behaviour ---


class TestLoadCached:
    def test_returns_none_when_file_missing(self, tmp_path):
        result = auth._load_cached(tmp_path / "nope.json", scopes=[])
        assert result is None

    def test_delegates_to_from_authorized_user_file_when_present(
        self, tmp_path, monkeypatch
    ):
        token_path = tmp_path / "token.json"
        token_path.write_text("{}")
        expected = _fake_creds(valid=True)
        load_mock = MagicMock(return_value=expected)
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.Credentials.from_authorized_user_file",
            load_mock,
        )

        result = auth._load_cached(token_path, scopes=["scope1"])

        load_mock.assert_called_once_with(str(token_path), ["scope1"])
        assert result is expected


# --- default settings (None passed) ---


class TestDefaultSettings:
    def test_uses_gmailsettings_from_env_when_none_passed(
        self, tmp_path, monkeypatch
    ):
        creds_path = tmp_path / "creds.json"
        creds_path.write_text("{}")
        token_path = tmp_path / "token.json"

        monkeypatch.setenv("GMAIL_CREDENTIALS_PATH", str(creds_path))
        monkeypatch.setenv("GMAIL_TOKEN_PATH", str(token_path))
        monkeypatch.setenv(
            "GMAIL_SCOPES", "https://www.googleapis.com/auth/gmail.readonly"
        )

        new_creds = _fake_creds(valid=True)
        flow_instance = MagicMock()
        flow_instance.run_local_server.return_value = new_creds
        monkeypatch.setattr(
            "zashiki_warasi.gmail.auth.InstalledAppFlow.from_client_secrets_file",
            MagicMock(return_value=flow_instance),
        )

        result = auth.get_credentials()  # no settings passed

        assert result is new_creds
        assert token_path.exists()
