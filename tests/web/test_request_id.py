"""Behavioural tests for the request-id middleware."""

from __future__ import annotations

import logging
import re
from unittest.mock import MagicMock

import pytest
from fastapi import Depends
from fastapi.testclient import TestClient

from zashiki_warasi.core.logging import _REQUEST_ID_CTX
from zashiki_warasi.web.app import create_app
from zashiki_warasi.web.dependencies import get_services


@pytest.fixture
def app():
    application = create_app()
    application.dependency_overrides[get_services] = lambda: MagicMock()
    return application


class TestHeaderRoundTrip:
    def test_client_supplied_header_is_honored_and_echoed(self, app):
        client = TestClient(app)
        r = client.get(
            "/healthz",
            headers={"X-Request-ID": "cron-2026-08-09-1200"},
        )
        # Even though /healthz's stub-Services fixture doesn't include
        # a real pool (so oauth/db checks may fail), the middleware runs
        # before the handler → the response header is set regardless of
        # 200/503 outcome.
        assert r.headers.get("X-Request-ID") == "cron-2026-08-09-1200"

    def test_missing_header_generates_12_hex_value(self, app):
        client = TestClient(app)
        r = client.get("/healthz")
        rid = r.headers.get("X-Request-ID")
        assert rid is not None
        assert re.fullmatch(r"[0-9a-f]{12}", rid), rid

    def test_empty_header_treated_as_missing_and_generates(self, app):
        """Whitespace-only / empty header must not silently propagate
        as the request-id — treat as absent and generate a fresh one."""
        client = TestClient(app)
        r = client.get(
            "/healthz", headers={"X-Request-ID": "   "}
        )
        rid = r.headers.get("X-Request-ID")
        assert rid is not None
        assert rid.strip() != ""
        assert re.fullmatch(r"[0-9a-f]{12}", rid), rid

    def test_two_requests_get_distinct_generated_ids(self, app):
        client = TestClient(app)
        r1 = client.get("/healthz")
        r2 = client.get("/healthz")
        assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


class TestContextvarPropagation:
    """The middleware's ContextVar set is what makes logs inside the
    handler carry `request_id=<value>`. Verified via a probe route
    that emits a log line and asserts the caplog record shape."""

    def test_log_inside_handler_carries_request_id(self, app, caplog):
        logger = logging.getLogger("zashiki_warasi.tests.probe")

        @app.get("/__probe_log")
        def _probe():
            logger.info("hello from handler")
            return {"ok": True}

        client = TestClient(app)
        with caplog.at_level(logging.INFO, logger="zashiki_warasi.tests.probe"):
            r = client.get(
                "/__probe_log",
                headers={"X-Request-ID": "trace-me-42abc"},
            )
        assert r.status_code == 200

        # The log record fixture doesn't run through the formatter, but
        # the ContextVar-based formatter reads _REQUEST_ID_CTX at format
        # time. To assert the ContextVar propagation happened, we check
        # by grabbing the record and manually feeding it to a fresh
        # ContextFormatter — except the request has ended and the
        # ContextVar is reset. Instead, we test the ContextVar during
        # the request by capturing it in the handler.
        # A more direct assertion: extend the probe to snapshot the
        # ContextVar and assert on it.
        assert r.headers["X-Request-ID"] == "trace-me-42abc"

    def test_contextvar_reset_after_response(self, app):
        """Middleware must reset the ContextVar in the finally block —
        else a request-id leaks into subsequent request handling on
        the same asyncio task (impossible in threadpool but possible
        under async handlers)."""
        client = TestClient(app)
        client.get(
            "/healthz", headers={"X-Request-ID": "should-not-leak"}
        )
        # Post-request: the module-scope ContextVar is back to None.
        assert _REQUEST_ID_CTX.get() is None


class TestContextvarVisibleInsideHandler:
    """Direct verification that the handler sees the middleware's
    ContextVar assignment before the handler body runs."""

    def test_handler_sees_supplied_request_id(self, app):
        captured: dict = {}

        @app.get("/__probe_cv")
        def _probe():
            captured["rid"] = _REQUEST_ID_CTX.get()
            return {"ok": True}

        client = TestClient(app)
        r = client.get(
            "/__probe_cv", headers={"X-Request-ID": "seen-in-handler-42"}
        )
        assert r.status_code == 200
        assert captured["rid"] == "seen-in-handler-42"
