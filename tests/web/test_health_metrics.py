"""Metric-emission tests for /healthz — verifies zashiki_healthz_status
gauge + zashiki_oauth_token_expires_in_seconds gauge are set on every
probe according to the check outcomes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.observability import REGISTRY
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


def _gauge_value(name: str) -> float | None:
    return REGISTRY.get_sample_value(name)


def _fake_services(*, db_ok: bool, creds) -> MagicMock:
    services = MagicMock(name="services")
    # DB pool: pool.connection().__enter__() → conn ; conn.cursor().__enter__() → cur
    if db_ok:
        cur = MagicMock()
        cur.__enter__.return_value = cur
        cur.__exit__.return_value = False
        conn = MagicMock()
        conn.__enter__.return_value = conn
        conn.__exit__.return_value = False
        conn.cursor.return_value = cur
        services.checkpointer_pool.connection.return_value = conn
    else:
        services.checkpointer_pool.connection.side_effect = RuntimeError(
            "db down"
        )
    services.credentials = creds
    return services


@pytest.fixture
def app():
    return create_app()


class TestHealthzStatusGauge:
    def test_healthy_sets_gauge_to_1(self, app):
        # naive-UTC datetime, per google.auth convention for
        # Credentials.expiry. Future-dated so oauth_ok is True.
        future_expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        creds = SimpleNamespace(
            expired=False,
            refresh_token="rt",
            expiry=future_expiry,
        )
        services = _fake_services(db_ok=True, creds=creds)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        r = client.get("/healthz")
        assert r.status_code == 200
        assert _gauge_value("zashiki_healthz_status") == 1.0

    def test_db_down_sets_gauge_to_0(self, app):
        future_expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1)
        creds = SimpleNamespace(
            expired=False,
            refresh_token="rt",
            expiry=future_expiry,
        )
        services = _fake_services(db_ok=False, creds=creds)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        r = client.get("/healthz")
        assert r.status_code == 503
        assert _gauge_value("zashiki_healthz_status") == 0.0

    def test_oauth_missing_sets_gauge_to_0(self, app):
        services = _fake_services(db_ok=True, creds=None)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        r = client.get("/healthz")
        assert r.status_code == 503
        assert _gauge_value("zashiki_healthz_status") == 0.0


class TestOAuthTokenExpiresInSecondsGauge:
    def test_positive_when_token_valid(self, app):
        # 3600s to expiry (naive UTC)
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(seconds=3600)
        creds = SimpleNamespace(
            expired=False, refresh_token="rt", expiry=expiry
        )
        services = _fake_services(db_ok=True, creds=creds)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        client.get("/healthz")
        gauge = _gauge_value("zashiki_oauth_token_expires_in_seconds")
        assert gauge is not None
        # allow slop for the fraction of a second the request takes
        assert 3595 < gauge <= 3600

    def test_negative_when_token_past_expiry(self, app):
        expiry = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=30)
        creds = SimpleNamespace(
            expired=True, refresh_token="rt", expiry=expiry
        )
        services = _fake_services(db_ok=True, creds=creds)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        client.get("/healthz")
        gauge = _gauge_value("zashiki_oauth_token_expires_in_seconds")
        assert gauge is not None
        assert -35 < gauge < -25  # ~-30 with slop

    def test_zero_when_no_credentials(self, app):
        services = _fake_services(db_ok=True, creds=None)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        client.get("/healthz")
        assert (
            _gauge_value("zashiki_oauth_token_expires_in_seconds") == 0.0
        )

    def test_zero_when_expiry_field_absent(self, app):
        # A credentials object without an `expiry` attribute (e.g. a
        # test double) SHALL yield 0.0 rather than blowing up.
        creds = SimpleNamespace(expired=False, refresh_token="rt")
        services = _fake_services(db_ok=True, creds=creds)
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)
        client.get("/healthz")
        assert (
            _gauge_value("zashiki_oauth_token_expires_in_seconds") == 0.0
        )
