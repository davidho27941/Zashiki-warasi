"""Behavioural tests for the OAuth web-flow endpoints.

`/auth/start` and `/auth/callback` are tested against a fake
OAuthFlowStore (dict-backed) plus a real `google_auth_oauthlib.Flow`
built from a fixture client-secrets JSON. The actual token exchange
call (`flow.fetch_token`) is monkeypatched — we don't want the tests
to hit Google's token endpoint.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from google_auth_oauthlib.flow import Flow

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


@pytest.fixture
def client_secrets_file(tmp_path) -> Path:
    path = tmp_path / "credentials.json"
    path.write_text(json.dumps(CLIENT_SECRETS))
    return path


class _DictFlowStore:
    """Fake OAuthFlowStore backed by a dict. Matches the two methods
    the endpoints call (put + pop). Behaves like the real store
    (pop deletes on hit + returns None on miss)."""

    def __init__(self) -> None:
        self._by_state: dict[str, Flow] = {}

    def put(self, state: str, flow: Flow) -> None:
        self._by_state[state] = flow

    def pop(self, state: str) -> Flow | None:
        return self._by_state.pop(state, None)


def _make_services(
    tmp_path: Path,
    client_secrets_file: Path,
    *,
    redirect_uri: str | None = "http://127.0.0.1:8080/auth/callback",
    api_key: str | None = None,
):
    services = MagicMock(name="services")
    services.http_settings.api_key = api_key
    services.oauth_settings.redirect_uri = redirect_uri
    services.gmail_settings.credentials_path = client_secrets_file
    services.gmail_settings.token_path = tmp_path / "token.json"
    services.gmail_settings.scopes = [
        "https://www.googleapis.com/auth/gmail.readonly"
    ]
    services.oauth_flow_store = _DictFlowStore()
    services.notifier = MagicMock()
    services.gmail_client = MagicMock()
    return services


@pytest.fixture
def app():
    return create_app()


# ---------- /auth/start ----------


class TestAuthStart:
    def test_unknown_csrf_returns_400(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get(
            "/auth/start?csrf=nope", follow_redirects=False
        )
        assert r.status_code == 400
        assert "unknown_or_expired_csrf" in r.json()["detail"]

    def test_missing_redirect_uri_returns_500(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(
            tmp_path, client_secrets_file, redirect_uri=None
        )
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get(
            "/auth/start?csrf=nope", follow_redirects=False
        )
        assert r.status_code == 500
        assert "OAUTH_REDIRECT_URI" in r.json()["detail"]

    def test_valid_csrf_redirects_to_google(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        # Pre-populate the store as /reauth would.
        flow = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=list(services.gmail_settings.scopes),
            state="csrf-1",
            redirect_uri=services.oauth_settings.redirect_uri,
        )
        services.oauth_flow_store.put("csrf-1", flow)
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).get(
            "/auth/start?csrf=csrf-1", follow_redirects=False
        )

        assert r.status_code == 307
        location = r.headers["location"]
        assert location.startswith("https://accounts.google.com/o/oauth2/")
        # The exact `state` we minted must round-trip through the URL
        # so /auth/callback can match on it.
        assert "state=csrf-1" in location

    def test_flow_is_put_back_after_redirect(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        flow = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=list(services.gmail_settings.scopes),
            state="csrf-2",
            redirect_uri=services.oauth_settings.redirect_uri,
        )
        services.oauth_flow_store.put("csrf-2", flow)
        app.dependency_overrides[get_services] = lambda: services

        TestClient(app).get(
            "/auth/start?csrf=csrf-2", follow_redirects=False
        )

        # The pop-then-put pattern leaves the Flow in the store so
        # the callback (possibly on a different replica) can find it.
        assert services.oauth_flow_store.pop("csrf-2") is not None


# ---------- /auth/callback ----------


class TestAuthCallback:
    def _prime_flow(
        self, services, client_secrets_file, state="csrf-cb", token=None
    ) -> Flow:
        flow = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=list(services.gmail_settings.scopes),
            state=state,
            redirect_uri=services.oauth_settings.redirect_uri,
        )
        # Fake fetch_token so no real Google request goes out.
        creds = MagicMock()
        creds.to_json.return_value = json.dumps(
            {"token": token or "access-token-xyz", "refresh_token": "rt"}
        )
        flow.fetch_token = MagicMock(return_value={"access_token": "a"})
        # Patch the `credentials` property via type-level descriptor
        # replacement is fragile; monkeypatch the attribute on the
        # instance instead using a class that returns creds.
        type(flow).credentials = property(lambda self: creds)  # type: ignore[assignment]
        services.oauth_flow_store.put(state, flow)
        return flow

    def test_unknown_state_returns_400(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get("/auth/callback?code=xyz&state=nope")
        assert r.status_code == 400
        assert "unknown_or_expired_state" in r.json()["detail"]
        # Token file MUST NOT be written on a bad state.
        assert not services.gmail_settings.token_path.exists()

    def test_valid_flow_writes_token_and_reloads(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        self._prime_flow(services, client_secrets_file, state="csrf-cb")
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).get("/auth/callback?code=code123&state=csrf-cb")

        assert r.status_code == 200
        # Token persisted at the configured path.
        assert services.gmail_settings.token_path.exists()
        # gmail_client.reload_credentials() called so subsequent API
        # calls use the new token.
        services.gmail_client.reload_credentials.assert_called_once()
        # Notify sent (best-effort success signal for the operator).
        services.notifier.send_message.assert_called_once()

    def test_fetch_token_failure_returns_400_no_write(
        self, app, tmp_path, client_secrets_file
    ):
        services = _make_services(tmp_path, client_secrets_file)
        flow = Flow.from_client_secrets_file(
            str(client_secrets_file),
            scopes=list(services.gmail_settings.scopes),
            state="csrf-fail",
            redirect_uri=services.oauth_settings.redirect_uri,
        )
        flow.fetch_token = MagicMock(
            side_effect=Exception("bad_verification_code")
        )
        services.oauth_flow_store.put("csrf-fail", flow)
        app.dependency_overrides[get_services] = lambda: services

        r = TestClient(app).get("/auth/callback?code=bad&state=csrf-fail")

        assert r.status_code == 400
        assert "fetch_token_failed" in r.json()["detail"]
        assert not services.gmail_settings.token_path.exists()
        services.gmail_client.reload_credentials.assert_not_called()
