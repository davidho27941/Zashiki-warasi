"""Behavioural tests for POST /poll.

Mocks the advisory_lock context manager + services.poller.tick_once
so we can assert single-flight behavior (same-loop concurrent → 409),
error propagation (uncaught → 500), and TickResult body shape.

The cross-connection integration test (two clients each with their
own connection racing on the real pg_try_advisory_lock) is deferred
to Group 17.6's cross-replica live smoke — the unit-level test here
uses the sentinel-returning fake lock to exercise the 409 path
deterministically.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.core.schemas import TickResult
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services
from zashiki_warasi.web.routers import poll as poll_module


def _fake_lock_cm(acquired: bool):
    """Build a fake advisory_lock context manager returning the given
    acquired flag. Structured to swap in via monkeypatch."""

    class _Cm:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return acquired, MagicMock(name="conn")

        def __exit__(self, *exc):
            return False

    return _Cm


def _services_with_tick(tick_result: TickResult):
    services = MagicMock(name="services")
    services.checkpointer_pool = MagicMock()
    services.poller = MagicMock()
    # tick_once(services.poller) delegates to services.poller.tick_once()
    services.poller.tick_once.return_value = tick_result
    services.http_settings.api_key = None  # auth disabled for these tests
    return services


@pytest.fixture
def app():
    return create_app()


@pytest.fixture
def result_ok():
    return TickResult(
        duration_ms=15,
        messages_processed=2,
        cursor_before=500,
        cursor_after=700,
        rebaselined=False,
        error=None,
    )


class TestSuccessfulTick:
    def test_returns_200_with_tickresult_shape(self, app, monkeypatch, result_ok):
        services = _services_with_tick(result_ok)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r = client.post("/poll")

        assert r.status_code == 200
        body = r.json()
        # All six TickResult keys present — the operator-facing contract.
        for key in (
            "duration_ms",
            "messages_processed",
            "cursor_before",
            "cursor_after",
            "rebaselined",
            "error",
        ):
            assert key in body
        assert body["messages_processed"] == 2
        assert body["cursor_after"] == 700

    def test_tick_once_called_via_delegate(self, app, monkeypatch, result_ok):
        services = _services_with_tick(result_ok)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        TestClient(app).post("/poll")

        # tick_once(services.poller) → services.poller.tick_once()
        services.poller.tick_once.assert_called_once()


class TestSingleFlight:
    def test_lock_not_acquired_returns_409(self, app, monkeypatch, result_ok):
        services = _services_with_tick(result_ok)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=False)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r = client.post("/poll")

        assert r.status_code == 409
        assert r.json() == {"reason": "tick_in_flight"}
        # tick_once must NOT have been called — the 409 is a real
        # short-circuit, not a "we ran the tick and then decided
        # to return 409" fake.
        services.poller.tick_once.assert_not_called()

    def test_sequential_calls_both_succeed(
        self, app, monkeypatch, result_ok
    ):
        """Two calls not overlapping → both acquire the lock → both 200."""
        services = _services_with_tick(result_ok)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r1 = client.post("/poll")
        r2 = client.post("/poll")

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert services.poller.tick_once.call_count == 2


class TestErrorPropagation:
    def test_uncaught_exception_returns_500(
        self, app, monkeypatch, result_ok
    ):
        """Uncaught exceptions from tick_once surface as HTTP 500 via
        FastAPI's default handler — NOT swallowed to a 200-with-error
        response. Recoverable failures still travel via TickResult.error;
        this covers the truly-unexpected case."""
        services = _services_with_tick(result_ok)
        services.poller.tick_once.side_effect = RuntimeError(
            "the world is on fire"
        )
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app, raise_server_exceptions=False)

        r = client.post("/poll")

        assert r.status_code == 500

    def test_recoverable_failure_returns_200_with_error_field(
        self, app, monkeypatch
    ):
        """Auth expiry / other recoverable in-tick errors surface as
        TickResult.error at 200 — the tick body caught them."""
        recoverable = TickResult(
            duration_ms=5,
            messages_processed=0,
            cursor_before=None,
            cursor_after=None,
            rebaselined=False,
            error="credential_refresh_failed: invalid_grant",
        )
        services = _services_with_tick(recoverable)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        r = client.post("/poll")

        assert r.status_code == 200
        assert r.json()["error"].startswith("credential_refresh_failed")
