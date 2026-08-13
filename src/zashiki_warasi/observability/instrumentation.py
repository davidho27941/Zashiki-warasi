"""Small context-manager helpers for the repeating latency+outcome
metric-emit pattern used across gmail / llm / telegram / oauth code
paths.

Usage:

    from zashiki_warasi.observability import (
        gmail_api_calls_total,
        gmail_api_latency_seconds,
    )
    from zashiki_warasi.observability.instrumentation import record_call

    with record_call(
        counter=gmail_api_calls_total,
        histogram=gmail_api_latency_seconds,
        counter_labels={"operation": "get"},
        histogram_labels={"operation": "get"},
    ):
        message = self._service.users().messages().get(...).execute()

Semantics:
- On normal exit, `counter.labels(**counter_labels, outcome="success").inc()`
  and `histogram.labels(**histogram_labels).observe(<seconds>)`.
- On exception, `counter.labels(**counter_labels, outcome="error").inc()`
  and the histogram still observes the elapsed time (so error latency
  is visible on dashboards); the exception is re-raised.
- The counter's labelset MUST include `outcome`; the helper appends it.
- The histogram MUST NOT include an `outcome` label (dashboards read
  latency across all outcomes as one distribution — split-by-outcome
  is a separate metric family if we ever want it).

Kept in a separate module from `metrics.py` so the metric contract file
stays declaration-only (easier to audit for cardinality).
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from prometheus_client import Counter, Histogram


@contextmanager
def record_call(
    *,
    counter: Counter,
    histogram: Histogram | None = None,
    counter_labels: dict[str, str] | None = None,
    histogram_labels: dict[str, str] | None = None,
) -> Iterator[None]:
    """Time a block, count its outcome, propagate exceptions."""
    counter_labels = dict(counter_labels or {})
    histogram_labels = dict(histogram_labels or {})
    started = time.monotonic()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        elapsed = time.monotonic() - started
        if histogram is not None:
            if histogram_labels:
                histogram.labels(**histogram_labels).observe(elapsed)
            else:
                histogram.observe(elapsed)
        merged = {**counter_labels, "outcome": outcome}
        counter.labels(**merged).inc()


@contextmanager
def observe_outcome(
    *, counter: Counter, extra_labels: dict[str, str] | None = None
) -> Iterator[None]:
    """Counter-only variant for call sites that don't need latency
    (telegram sends, oauth refresh — either succeed or fail, timing
    doesn't inform any alert we plan to write).

    Increments `counter.labels(**extra_labels, outcome=<success|error>)`
    exactly once on block exit.
    """
    extra_labels = dict(extra_labels or {})
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        merged = {**extra_labels, "outcome": outcome}
        counter.labels(**merged).inc()
