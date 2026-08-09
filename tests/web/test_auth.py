"""Behavioural tests for X-API-Key auth on /poll and /reauth.

The bootstrap-time fail-fast (non-loopback bind + missing key → refuse
to start) is enforced by the HttpSettings model_validator and tested
in tests/core/test_config.py — no need to re-verify here. These tests
cover the per-request auth check only.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.core.schemas import TickResult
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services
from zashiki_warasi.web.routers import poll as poll_module


def _services(api_key: str | None):
    services = MagicMock(name="services")
    services.http_settings.api_key = api_key
    # Neutral defaults so the underlying handler doesn't crash if
    # auth lets the request through.
    services.checkpointer_pool = MagicMock()
    services.poller = MagicMock()
    services.poller.tick_once.return_value = TickResult(
        duration_ms=1,
        messages_processed=0,
        cursor_before=None,
        cursor_after=None,
    )
    return services


def _fake_lock_acquired():
    class _Cm:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return True, MagicMock()

        def __exit__(self, *exc):
            return False

    return _Cm


@pytest.fixture
def app():
    return create_app()


class TestAuthDisabled:
    """When api_key is unset / empty, the header is ignored — safe
    only because HTTP_BIND_HOST=127.0.0.1 is the default."""

    def test_no_header_ok(self, app, monkeypatch):
        services = _services(api_key=None)
        monkeypatch.setattr(poll_module, "advisory_lock", _fake_lock_acquired())
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/poll")
        assert r.status_code == 200

    def test_wrong_header_ignored(self, app, monkeypatch):
        services = _services(api_key=None)
        monkeypatch.setattr(poll_module, "advisory_lock", _fake_lock_acquired())
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/poll", headers={"X-API-Key": "wrong"})
        assert r.status_code == 200

    def test_empty_string_key_is_disabled(self, app, monkeypatch):
        services = _services(api_key="")
        monkeypatch.setattr(poll_module, "advisory_lock", _fake_lock_acquired())
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/poll")
        assert r.status_code == 200


class TestAuthEnforced:
    def test_missing_header_returns_401(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/poll")
        assert r.status_code == 401
        assert "invalid_or_missing_api_key" in r.json()["detail"]

    def test_wrong_header_returns_401(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post(
            "/poll", headers={"X-API-Key": "not-the-right-one"}
        )
        assert r.status_code == 401

    def test_correct_header_returns_200(self, app, monkeypatch):
        services = _services(api_key="shhh")
        monkeypatch.setattr(poll_module, "advisory_lock", _fake_lock_acquired())
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/poll", headers={"X-API-Key": "shhh"})
        assert r.status_code == 200


class TestReauthProtected:
    """/reauth is protected identically to /poll. Body currently is
    the 501 stub from Group 3; auth check runs before the handler."""

    def test_reauth_no_header_401_when_key_set(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).post("/reauth")
        assert r.status_code == 401


class TestPublicEndpointsUnaffected:
    """/healthz + /auth/* MUST NOT require the API key. Probes need
    /healthz open; OAuth callbacks come from the operator's browser
    and can't send arbitrary headers."""

    def test_healthz_open_even_with_key_configured(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get("/healthz")
        # /healthz returns 200 or 503 depending on the mock; either
        # way it's NOT 401.
        assert r.status_code != 401

    def test_auth_start_open_even_with_key_configured(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get("/auth/start?csrf=abc")
        # Stub returns 501 today; the point is it's not 401.
        assert r.status_code != 401

    def test_auth_callback_open_even_with_key_configured(self, app):
        services = _services(api_key="shhh")
        app.dependency_overrides[get_services] = lambda: services
        r = TestClient(app).get("/auth/callback?code=xyz&state=abc")
        assert r.status_code != 401
