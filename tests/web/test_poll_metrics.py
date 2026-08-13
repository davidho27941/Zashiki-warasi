"""Metric-emission tests for POST /poll.

Uses the same _fake_lock_cm + _services_with_tick pattern as
test_poll.py but focuses on the observability contract: which counters
increment on which outcomes, and whether tick_duration_seconds actually
observes.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.core.schemas import TickResult
from zashiki_warasi.observability import (
    REGISTRY,
    tick_conflicts_total,
    tick_duration_seconds,
    tick_messages_processed_total,
    tick_rebaseline_total,
)
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services
from zashiki_warasi.web.routers import poll as poll_module


def _fake_lock_cm(acquired: bool):
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
    services.poller.tick_once.return_value = tick_result
    services.http_settings.api_key = None
    return services


def _counter_value(name: str, labels: dict[str, str] | None = None) -> float:
    """Look up a labelled Counter's current value from the REGISTRY."""
    return REGISTRY.get_sample_value(name, labels=labels) or 0.0


def _histogram_count(
    name: str, labels: dict[str, str] | None = None
) -> float:
    """Look up a labelled Histogram's `_count` (i.e. number of
    observations recorded)."""
    return REGISTRY.get_sample_value(f"{name}_count", labels=labels) or 0.0


@pytest.fixture
def app():
    return create_app()


class TestSuccessfulTickMetrics:
    def test_observes_tick_duration_with_success_outcome(
        self, app, monkeypatch
    ):
        result = TickResult(
            duration_ms=150,
            messages_processed=2,
            cursor_before=500,
            cursor_after=700,
            rebaselined=False,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before_count = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "success"}
        )
        before_msgs = _counter_value(
            "zashiki_tick_messages_processed_total"
        )
        r = client.post("/poll")
        assert r.status_code == 200

        after_count = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "success"}
        )
        after_msgs = _counter_value(
            "zashiki_tick_messages_processed_total"
        )
        assert after_count == before_count + 1
        assert after_msgs == before_msgs + 2  # result.messages_processed

    def test_success_does_not_increment_error_outcome(
        self, app, monkeypatch
    ):
        result = TickResult(
            duration_ms=10,
            messages_processed=0,
            cursor_before=100,
            cursor_after=100,
            rebaselined=False,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before_error = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )
        client.post("/poll")
        after_error = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )
        assert after_error == before_error


class TestErrorTickMetrics:
    def test_error_result_observes_error_outcome(self, app, monkeypatch):
        result = TickResult(
            duration_ms=30,
            messages_processed=0,
            cursor_before=None,
            cursor_after=None,
            rebaselined=False,
            error="credential_refresh_failed: token dead",
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )
        r = client.post("/poll")
        assert r.status_code == 200  # error surfaces in body, not 500
        after = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )
        assert after == before + 1


class TestConflictMetrics:
    def test_409_increments_conflicts_and_does_not_observe_duration(
        self, app, monkeypatch
    ):
        # Lock never acquired → 409 path
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=False)
        )
        services = _services_with_tick(
            TickResult(
                duration_ms=0,
                messages_processed=0,
                cursor_before=None,
                cursor_after=None,
                rebaselined=False,
                error=None,
            )
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before_conflicts = _counter_value("zashiki_tick_conflicts_total")
        before_duration_success = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "success"}
        )
        before_duration_error = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )

        r = client.post("/poll")
        assert r.status_code == 409

        after_conflicts = _counter_value("zashiki_tick_conflicts_total")
        after_duration_success = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "success"}
        )
        after_duration_error = _histogram_count(
            "zashiki_tick_duration_seconds", {"outcome": "error"}
        )

        assert after_conflicts == before_conflicts + 1
        assert after_duration_success == before_duration_success
        assert after_duration_error == before_duration_error


class TestRebaselineMetrics:
    def test_rebaselined_flag_increments_counter(self, app, monkeypatch):
        result = TickResult(
            duration_ms=25,
            messages_processed=0,
            cursor_before=None,
            cursor_after=1234,
            rebaselined=True,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before = _counter_value("zashiki_tick_rebaseline_total")
        client.post("/poll")
        after = _counter_value("zashiki_tick_rebaseline_total")
        assert after == before + 1

    def test_no_rebaseline_does_not_increment(self, app, monkeypatch):
        result = TickResult(
            duration_ms=25,
            messages_processed=1,
            cursor_before=100,
            cursor_after=101,
            rebaselined=False,
            error=None,
        )
        services = _services_with_tick(result)
        monkeypatch.setattr(
            poll_module, "advisory_lock", _fake_lock_cm(acquired=True)
        )
        app.dependency_overrides[get_services] = lambda: services
        client = TestClient(app)

        before = _counter_value("zashiki_tick_rebaseline_total")
        client.post("/poll")
        after = _counter_value("zashiki_tick_rebaseline_total")
        assert after == before  # unchanged
