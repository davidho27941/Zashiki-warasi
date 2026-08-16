"""GET /metrics endpoint tests."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


@pytest.fixture
def app():
    application = create_app()
    application.dependency_overrides[get_services] = lambda: MagicMock()
    return application


class TestMetricsEndpoint:
    def test_returns_200_and_prometheus_content_type(self, app):
        client = TestClient(app)
        r = client.get("/metrics")
        assert r.status_code == 200
        # prometheus_client emits CONTENT_TYPE_LATEST which currently
        # resolves to `text/plain; version=1.0.0; charset=utf-8` (OpenMetrics
        # 1.0.0 exposition). We assert only the media-type prefix + presence
        # of a version token so the test doesn't flap when prometheus_client
        # bumps the version format.
        content_type = r.headers["content-type"]
        assert content_type.startswith("text/plain")
        assert "version=" in content_type
        assert "charset=utf-8" in content_type

    def test_body_contains_at_least_one_help_and_type_line(self, app):
        client = TestClient(app)
        r = client.get("/metrics")
        body = r.text
        assert "# HELP " in body
        assert "# TYPE " in body

    def test_body_contains_contracted_zashiki_families(self, app):
        client = TestClient(app)
        r = client.get("/metrics")
        body = r.text
        # Spot-check a few families across counter / gauge / histogram
        # to catch regressions where the whole observability import
        # chain silently breaks (empty scrape).
        assert "zashiki_tick_conflicts_total" in body
        assert "zashiki_healthz_status" in body
        assert "zashiki_gmail_api_latency_seconds" in body
        assert "zashiki_traces_dropped_total" in body

    def test_no_zashiki_http_requests_family(self, app):
        """Regression pin for design D16 — the /metrics response must
        not contain a generic HTTP request counter."""
        client = TestClient(app)
        r = client.get("/metrics")
        assert "zashiki_http_requests" not in r.text

    def test_no_api_key_gate_on_metrics(self, app, monkeypatch):
        """Even with HTTP_API_KEY set, /metrics is reachable without
        the X-API-Key header — scrapers can't inject arbitrary
        headers, and /metrics exposes no privileged data."""
        monkeypatch.setenv("HTTP_API_KEY", "secret-abc")
        # Re-create app so HttpSettings picks up the env var.
        application = create_app()
        application.dependency_overrides[get_services] = lambda: MagicMock()
        client = TestClient(application)

        r = client.get("/metrics")  # no X-API-Key header
        assert r.status_code == 200
        assert "# HELP" in r.text

    def test_metrics_endpoint_omitted_from_openapi_schema(self, app):
        client = TestClient(app)
        openapi = client.get("/openapi.json").json()
        # include_in_schema=False on the route
        assert "/metrics" not in openapi.get("paths", {})

    def test_metrics_request_carries_request_id_header(self, app):
        """RequestIdMiddleware wraps every HTTP route including /metrics
        (it's the same pure-ASGI middleware). This is a regression pin
        for the middleware conversion — a break in the pure-ASGI send
        wrapper would surface here first."""
        client = TestClient(app)
        r = client.get("/metrics")
        assert r.headers.get("X-Request-ID") is not None
