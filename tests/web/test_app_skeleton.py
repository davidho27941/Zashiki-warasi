"""Session-1 smoke tests for the FastAPI skeleton.

The lifespan is expensive (opens a Postgres pool) and can't run in
unit tests without a live DB, so these tests use `create_app()` +
manual `dependency_overrides` to inject a mock Services container.
The lifespan is deliberately skipped by never entering the TestClient
context manager; the app object serves routes without startup.

Groups 5 / 6 / 9 / 10 replace the placeholder handlers here with real
implementations plus their own end-to-end tests.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def mock_services():
    """A MagicMock stand-in for the Services container. Individual
    tests can attribute-assign fields on it; the placeholder handlers
    don't touch any fields yet."""
    return MagicMock(name="services")


@pytest.fixture
def client(app, mock_services):
    """TestClient WITHOUT entering the lifespan (no real Postgres)."""
    app.dependency_overrides[get_services] = lambda: mock_services
    return TestClient(app)


class TestEndpointRegistration:
    """Every documented endpoint from the http-service capability
    responds with something (not 404). Real bodies land in later
    groups; here we're pinning the URL contract only."""

    def test_healthz_registered(self, client, mock_services):
        # Configure the mock so /healthz's real checks (from Group 5)
        # can run: db pool.connection() as CM + credentials attrs.
        cursor = MagicMock()
        cursor.fetchone.return_value = (1,)
        cursor_cm = MagicMock()
        cursor_cm.__enter__.return_value = cursor
        conn = MagicMock()
        conn.cursor.return_value = cursor_cm
        conn_cm = MagicMock()
        conn_cm.__enter__.return_value = conn
        mock_services.checkpointer_pool.connection.return_value = conn_cm
        mock_services.credentials.expired = False
        mock_services.credentials.refresh_token = "r"

        r = client.get("/healthz")
        assert r.status_code == 200
        assert r.json()["status"] == "healthy"

    def test_auth_start_registered_but_not_implemented(self, client):
        # /auth/* stays a stub until Group 9.
        r = client.get("/auth/start?csrf=abc")
        assert r.status_code == 501
        assert "not_implemented" in r.json()["detail"]

    def test_auth_callback_registered_but_not_implemented(self, client):
        r = client.get("/auth/callback?code=xyz&state=abc")
        assert r.status_code == 501
        assert "not_implemented" in r.json()["detail"]

    def test_undocumented_path_is_404(self, client):
        r = client.get("/does-not-exist")
        assert r.status_code == 404

    # Note: /poll (Group 6) and /reauth (still a stub until Group 10,
    # but already protected by require_api_key) have dedicated test
    # modules (tests/web/test_poll.py, tests/web/test_auth.py).


class TestServicesDependency:
    """`get_services` is the canonical seam handlers pull the Services
    container through. dependency_overrides is the mocking contract."""

    def test_override_injects_stand_in(self, app, mock_services):
        """Overriding get_services should make the injected value flow
        into any handler that Depends() on it. Pinned via a probe route
        so the contract can't silently break with a future refactor."""
        from fastapi import Depends

        @app.get("/__probe")
        def _probe(services=Depends(get_services)):
            return {"is_mock": services is mock_services}

        app.dependency_overrides[get_services] = lambda: mock_services
        # No `with TestClient` — the lifespan would open a real pool.
        c = TestClient(app)
        r = c.get("/__probe")
        assert r.status_code == 200
        assert r.json() == {"is_mock": True}


class TestAppMetadata:
    """FastAPI-level configuration that affects operator UX."""

    def test_app_has_title(self):
        app = create_app()
        assert app.title == "Zashiki-warasi"

    def test_openapi_docs_reachable(self, client):
        r = client.get("/openapi.json")
        assert r.status_code == 200
        spec = r.json()
        paths = spec["paths"]
        assert "/healthz" in paths
        assert "/poll" in paths
        assert "/reauth" in paths
        assert "/auth/start" in paths
        assert "/auth/callback" in paths
