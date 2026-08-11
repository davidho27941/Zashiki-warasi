"""Behavioural tests for /healthz.

Uses `create_app()` + dependency-overrides to inject a mock Services
container with controllable DB + credentials states. No real Postgres,
no real Gmail — pure unit-level truth of the response shape.
"""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


def _make_healthy_services(*, db_ok: bool = True, oauth_ok: bool = True):
    """Assemble a mock Services with configurable DB + OAuth states."""
    services = MagicMock(name="services")

    # DB: pool.connection() -> conn -> cursor(). We use the with-cm
    # protocol, so mimic it.
    cursor = MagicMock()
    if db_ok:
        cursor.execute.return_value = None
        cursor.fetchone.return_value = (1,)
    else:
        cursor.execute.side_effect = RuntimeError("db is down")

    cursor_cm = MagicMock()
    cursor_cm.__enter__.return_value = cursor
    cursor_cm.__exit__.return_value = False

    conn = MagicMock()
    conn.cursor.return_value = cursor_cm

    conn_cm = MagicMock()
    conn_cm.__enter__.return_value = conn
    conn_cm.__exit__.return_value = False

    services.checkpointer_pool.connection.return_value = conn_cm

    # OAuth: credentials object with expired + refresh_token attrs.
    if oauth_ok:
        creds = MagicMock()
        creds.expired = False
        creds.refresh_token = "refresh-token-value"
        services.credentials = creds
    else:
        services.credentials = None

    return services


@pytest.fixture
def app():
    return create_app()


class TestHealthzHappyPath:
    def test_all_ok_returns_200(self, app):
        services = _make_healthy_services()
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 200
        assert r.json() == {
            "status": "healthy",
            "checks": {"db": True, "oauth": True},
        }


class TestHealthzUnhealthyPaths:
    def test_db_down_returns_503(self, app):
        services = _make_healthy_services(db_ok=False, oauth_ok=True)
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 503
        assert r.json() == {
            "status": "unhealthy",
            "checks": {"db": False, "oauth": True},
        }

    def test_oauth_missing_returns_503(self, app):
        services = _make_healthy_services(db_ok=True, oauth_ok=False)
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 503
        body = r.json()
        assert body["checks"]["db"] is True
        assert body["checks"]["oauth"] is False

    def test_both_down_returns_503_with_both_false(self, app):
        services = _make_healthy_services(db_ok=False, oauth_ok=False)
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 503
        assert r.json()["checks"] == {"db": False, "oauth": False}


class TestOauthCheckSemantics:
    """The OAuth check is local-only — never calls Google. The truth
    is 'credentials present AND (not expired OR has refresh_token)'."""

    def test_expired_creds_with_refresh_token_are_healthy(self, app):
        services = _make_healthy_services(db_ok=True)
        services.credentials.expired = True
        services.credentials.refresh_token = "still-here"
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 200
        assert r.json()["checks"]["oauth"] is True

    def test_expired_creds_without_refresh_token_are_unhealthy(self, app):
        services = _make_healthy_services(db_ok=True)
        services.credentials.expired = True
        services.credentials.refresh_token = None
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 503
        assert r.json()["checks"]["oauth"] is False

    def test_fresh_creds_without_refresh_token_still_ok(self, app):
        """Non-expired credentials are healthy even without a refresh
        token — the current access token is still valid. (This is the
        first-startup case before any refresh has happened.)"""
        services = _make_healthy_services(db_ok=True)
        services.credentials.expired = False
        services.credentials.refresh_token = None
        app.dependency_overrides[get_services] = lambda: services
        # No `with TestClient(app)`: the lifespan would open a real
        # Postgres pool. Bare instantiation serves routes without
        # startup — override handles dependency injection.
        client = TestClient(app)
        r = client.get("/healthz")

        assert r.status_code == 200
        assert r.json()["checks"]["oauth"] is True
