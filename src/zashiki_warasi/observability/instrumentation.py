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


# --- OTel span helpers -----------------------------------------------
#
# These wrap `tracer.start_as_current_span` in patterns specific to our
# span-name contract (`zashiki.*`). Under OTEL_ENABLED=0 the tracer
# provider is NoOp and every helper here reduces to a few microseconds
# of function-call overhead — no allocations, no exports.


@contextmanager
def zashiki_span(
    name: str,
    *,
    attributes: dict[str, object] | None = None,
) -> Iterator[object]:
    """Open a `zashiki.<name>` OTel span. Yields the span so the
    caller can set attributes discovered during the block body
    (e.g. TickResult fields on the tick_once span).

    On exception, marks the span ERROR + records the exception and
    re-raises. Attribute-set errors (rare — usually attribute-type
    mismatches) are swallowed with a debug log so telemetry never
    bubbles up as a request failure.
    """
    from opentelemetry import trace
    from opentelemetry.trace import Status, StatusCode

    tracer = trace.get_tracer("zashiki_warasi")
    with tracer.start_as_current_span(f"zashiki.{name}") as span:
        if attributes:
            for k, v in attributes.items():
                if v is not None:
                    _safe_set_attribute(span, k, v)
        try:
            yield span
        except BaseException as exc:
            span.set_status(Status(StatusCode.ERROR, str(exc)))
            span.record_exception(exc)
            raise


def _safe_set_attribute(span, key: str, value: object) -> None:
    """set_attribute wrapper that swallows type-validation errors.

    OTel's Attribute type accepts str / bool / int / float / seq of
    same. A caller passing e.g. a datetime through by mistake would
    raise inside the SDK — we don't want telemetry to break a
    request path, so degrade to str() and log.
    """
    try:
        span.set_attribute(key, value)
    except Exception:
        try:
            span.set_attribute(key, str(value))
        except Exception:
            pass  # Give up silently.


def set_gen_ai_attributes(
    span, *, system: str, model: str, response: object | None = None
) -> None:
    """Attach OTel GenAI semantic-convention attributes to a live
    span. `system` is the provider identifier (`openai`, `anthropic`);
    `model` is the requested model name. `response` — if provided —
    is inspected for token-usage metadata (LangChain-shaped:
    `usage_metadata` on AIMessage, or `response_metadata.token_usage`
    on older paths). Silently skips fields the response doesn't
    expose — structured-output models often strip usage from the
    returned pydantic instance.
    """
    _safe_set_attribute(span, "gen_ai.system", system)
    _safe_set_attribute(span, "gen_ai.request.model", model)
    if response is None:
        return
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        rm = getattr(response, "response_metadata", None) or {}
        if isinstance(rm, dict):
            usage = rm.get("token_usage") or rm.get("usage")
    if not usage:
        return
    input_tokens = None
    output_tokens = None
    if isinstance(usage, dict):
        input_tokens = usage.get("input_tokens") or usage.get(
            "prompt_tokens"
        )
        output_tokens = usage.get("output_tokens") or usage.get(
            "completion_tokens"
        )
    else:
        input_tokens = getattr(usage, "input_tokens", None) or getattr(
            usage, "prompt_tokens", None
        )
        output_tokens = getattr(
            usage, "output_tokens", None
        ) or getattr(usage, "completion_tokens", None)
    if input_tokens is not None:
        _safe_set_attribute(span, "gen_ai.usage.input_tokens", input_tokens)
    if output_tokens is not None:
        _safe_set_attribute(span, "gen_ai.usage.output_tokens", output_tokens)
