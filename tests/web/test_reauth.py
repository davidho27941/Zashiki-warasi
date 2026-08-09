"""Behavioural tests for POST /reauth."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


CLIENT_SECRETS = {
    "installed": {
        "client_id": "test-client-id.apps.googleusercontent.com",
        "project_id": "test-project",
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
        "client_secret": "test-client-secret",
        "redirect_uris": ["http://127.0.0.1:8080/auth/callback"],
    }
}


class _DictFlowStore:
    def __init__(self) -> None:
        self._by_state = {}

    def put(self, state, flow):
        self._by_state[state] = flow

    def pop(self, state):
        return self._by_state.pop(state, None)


@pytest.fixture
def client_secrets_file(tmp_path) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(CLIENT_SECRETS))
    return path


def _make_services(
    client_secrets_file,
    *,
    redirect_uri="http://127.0.0.1:8080/auth/callback",
    api_key=None,
):
    services = MagicMock(name="services")
    services.http_settings.api_key = api_key
    services.oauth_settings.redirect_uri = redirect_uri
    services.gmail_settings.credentials_path = client_secrets_file
    services.gmail_settings.scopes = [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]
    services.oauth_flow_store = _DictFlowStore()
    return services


@pytest.fixture
def app():
    return create_app()


class TestReauthInitiation:
    def test_returns_auth_url_shape(self, app, client_secrets_file):
        services = _make_services(client_secrets_file)
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).post("/reauth")

        assert r.status_code == 200
        body = r.json()
        assert "auth_url" in body
        assert "state" in body
        assert body["expires_in"] == 600
        # The auth_url points at our own /auth/start with the csrf
        # query param.
        assert "/auth/start?csrf=" in body["auth_url"]
        # csrf in the URL equals the returned state (single source of
        # truth for the operator).
        assert body["state"] in body["auth_url"]

    def test_missing_redirect_uri_returns_500(
        self, app, client_secrets_file
    ):
        services = _make_services(client_secrets_file, redirect_uri=None)
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).post("/reauth")

        assert r.status_code == 500
        assert "OAUTH_REDIRECT_URI" in r.json()["detail"]

    def test_two_calls_return_distinct_states(self, app, client_secrets_file):
        services = _make_services(client_secrets_file)
        app.dependency_overrides[get_services] = lambda: services

        r1 = TestClient(app).post("/reauth")
        r2 = TestClient(app).post("/reauth")

        assert r1.json()["state"] != r2.json()["state"]
        # Both flows are persisted; store has both entries.
        assert services.oauth_flow_store.pop(r1.json()["state"]) is not None
        assert services.oauth_flow_store.pop(r2.json()["state"]) is not None

    def test_info_log_includes_state_prefix(
        self, app, client_secrets_file, caplog
    ):
        services = _make_services(client_secrets_file)
        app.dependency_overrides[get_services] = lambda: services

        with caplog.at_level(
            logging.INFO, logger="zashiki_warasi.web.routers.reauth"
        ):
            r = TestClient(app).post("/reauth")

        state_prefix = r.json()["state"][:8]
        assert any(
            "oauth: reauth flow initiated" in rec.getMessage()
            and state_prefix in rec.getMessage()
            for rec in caplog.records
        )


class TestReauthAuth:
    """API-key protection carries over from Group 8's require_api_key
    dependency — the /reauth-specific auth path is exercised in
    tests/web/test_auth.py::TestReauthProtected. Regression pin here."""

    def test_missing_header_returns_401_when_key_set(
        self, app, client_secrets_file
    ):
        services = _make_services(client_secrets_file, api_key="secret-abc")
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).post("/reauth")

        assert r.status_code == 401
