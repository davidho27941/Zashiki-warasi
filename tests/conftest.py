"""Session-wide pytest fixtures.

Autouse fixtures here run for every test in the suite. Keep the list
short and defensible — anything added here shapes the default
environment every test sees, so surprises show up as puzzling
failures across unrelated files.

Current contents:

- `_default_otel_disabled` — ensures `OTEL_ENABLED=0` for every test.
  Prevents accidental OTLP export attempts (against a non-existent
  local collector) and the rate-limited WARNING noise that would
  follow. Tests that specifically exercise OTel-enabled behavior
  should override with `monkeypatch.setenv("OTEL_ENABLED", "1")`
  inside their own body — the local monkeypatch wins over the
  autouse setup here (env is set at setup, monkeypatch mutates it
  during test body, monkeypatch teardown restores it before we
  reset).

Implementation note: this fixture DELIBERATELY does not use pytest's
`monkeypatch` fixture. Requesting `monkeypatch` here adds a fixture-
graph dependency that changes teardown ordering across the suite —
which specifically broke `tests/core/test_db.py`'s `_clear_lru_caches`
autouse fixture (its teardown started running before the per-test
`monkeypatch.setattr(db, "get_engine", ...)` undo, so `get_engine`
was still a lambda without `.cache_clear()`). Direct `os.environ`
manipulation with a try/finally sidesteps the dependency graph.
"""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _default_otel_disabled():
    """Force OTEL_ENABLED=0 for every test by default (see module docstring)."""
    prior = os.environ.get("OTEL_ENABLED")
    os.environ["OTEL_ENABLED"] = "0"
    try:
        yield
    finally:
        if prior is None:
            os.environ.pop("OTEL_ENABLED", None)
        else:
            os.environ["OTEL_ENABLED"] = prior
