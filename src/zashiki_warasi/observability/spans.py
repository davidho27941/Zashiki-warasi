"""Runtime span helpers for the v1.5.0 graph-node latency contract.

Three context managers wrap the three latency dimensions the spec cares
about:

- `graph_span(node, vertical)` — a LangGraph node execution.
- `llm_call(purpose, model=...)` — a single LLM invocation.
- `api_call(service, operation)` — a single outbound Google/Telegram call.

Each opens an OTel span (NoOp when `OTEL_ENABLED=0`, so no-cost when
tracing is off) and emits exactly one Prometheus histogram observation
on exit. The two share the same monotonic-clock read so "the trace
says 2.3s but the metric says 2.1s" can never happen.

End-to-end latency is a separate one-shot observation (`observe_email_end_to_end`)
rather than a context manager because the start timestamp comes from
Gmail's `internalDate`, not from a wrapping block.

The context managers yield a small mutable context object so callers
can:

- explicitly set `outcome = "skipped"` for early returns that aren't
  errors (regex short-circuits, dedup hits, etc.) — the default is
  `"success"`, and exceptions auto-flip to `"error"`.
- attach `prompt_tokens` / `completion_tokens` (LLM) or `status_code`
  (API) after the wrapped call returns.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from opentelemetry import trace

from zashiki_warasi.observability.metrics import (
    email_end_to_end_duration_seconds,
    external_api_duration_seconds,
    graph_node_duration_seconds,
    llm_call_duration_seconds,
)

# Instrumentation-scope names for the three tracers. Kept stable so
# Tempo queries can filter by `otel.scope.name`.
_TRACER_GRAPH = trace.get_tracer("zashiki.graph")
_TRACER_LLM = trace.get_tracer("zashiki.llm")
_TRACER_API = trace.get_tracer("zashiki.api")


@dataclass
class NodeSpanCtx:
    """Handle yielded by `graph_span`. Callers may set `outcome` to
    `"skipped"` before returning from the wrapped block to distinguish
    intentional early-return paths (short-circuit, dedup hit, missing
    field) from happy-path executions. `span` is exposed for callers
    that want to attach domain-specific attributes."""

    outcome: str = "success"
    span: object | None = None


@dataclass
class LLMCallCtx:
    """Handle yielded by `llm_call`. Callers SHOULD attach token counts
    after the LLM response returns so the OTel span carries them as
    `zashiki.llm.*` attributes. Missing counts are silently omitted
    from the span (they are optional in the response body of some
    backends). `span` is exposed so callers can pass it to
    `set_gen_ai_attributes(...)` for the OTel-standard `gen_ai.*`
    attribute set."""

    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    span: object | None = None


@dataclass
class APICallCtx:
    """Handle yielded by `api_call`. Callers SHOULD set `status_code`
    after the HTTP response returns (or in the exception handler for
    HTTPError-shape exceptions). The value lands on the span as
    `http.status_code`."""

    status_code: int | None = None
    span: object | None = None


@contextmanager
def graph_span(node: str, vertical: str) -> Iterator[NodeSpanCtx]:
    """Time and trace one LangGraph node execution.

    Emits one `zashiki_graph_node_duration_seconds{node, vertical,
    outcome}` observation and one OTel span `graph.{vertical}.{node}`
    with matching timestamps. Exceptions propagate; before propagation
    the outcome is flipped to `"error"` so the histogram bucket lands
    correctly and the operator can see the error rate per node.
    """
    ctx = NodeSpanCtx()
    start = time.monotonic()
    with _TRACER_GRAPH.start_as_current_span(f"graph.{vertical}.{node}") as span:
        ctx.span = span
        span.set_attribute("zashiki.node", node)
        span.set_attribute("zashiki.vertical", vertical)
        try:
            yield ctx
        except Exception:
            ctx.outcome = "error"
            raise
        finally:
            duration = time.monotonic() - start
            span.set_attribute("zashiki.outcome", ctx.outcome)
            graph_node_duration_seconds.labels(
                node=node, vertical=vertical, outcome=ctx.outcome
            ).observe(duration)


@contextmanager
def llm_call(purpose: str, *, model: str = "unknown") -> Iterator[LLMCallCtx]:
    """Time and trace one LLM invocation.

    Emits one `zashiki_llm_call_duration_seconds{purpose, model}`
    observation and one OTel sub-span `llm.{purpose}`. Token counts, if
    attached by the caller before the block exits, land as span
    attributes. Exceptions propagate — the histogram still records the
    failure duration so slow-then-fail vs fast-fail can be distinguished.
    """
    ctx = LLMCallCtx()
    start = time.monotonic()
    with _TRACER_LLM.start_as_current_span(f"llm.{purpose}") as span:
        ctx.span = span
        span.set_attribute("zashiki.llm.purpose", purpose)
        span.set_attribute("zashiki.llm.model", model)
        try:
            yield ctx
        finally:
            duration = time.monotonic() - start
            if ctx.prompt_tokens is not None:
                span.set_attribute("zashiki.llm.prompt_tokens", ctx.prompt_tokens)
            if ctx.completion_tokens is not None:
                span.set_attribute(
                    "zashiki.llm.completion_tokens", ctx.completion_tokens
                )
            llm_call_duration_seconds.labels(
                purpose=purpose, model=model
            ).observe(duration)


@contextmanager
def api_call(service: str, operation: str) -> Iterator[APICallCtx]:
    """Time and trace one outbound Google / Telegram HTTP call.

    Emits one `zashiki_external_api_duration_seconds{service, operation}`
    observation and one OTel sub-span `api.{service}.{operation}`.
    Callers SHOULD set `ctx.status_code` from the response (or in the
    exception handler for HTTPError-shape exceptions) so the span
    carries `http.status_code`.
    """
    ctx = APICallCtx()
    start = time.monotonic()
    with _TRACER_API.start_as_current_span(f"api.{service}.{operation}") as span:
        ctx.span = span
        span.set_attribute("zashiki.service", service)
        span.set_attribute("zashiki.operation", operation)
        try:
            yield ctx
        finally:
            duration = time.monotonic() - start
            if ctx.status_code is not None:
                span.set_attribute("http.status_code", ctx.status_code)
            external_api_duration_seconds.labels(
                service=service, operation=operation
            ).observe(duration)


def observe_email_end_to_end(
    category: str, outcome: str, duration_seconds: float
) -> None:
    """Record one observation into
    `zashiki_email_end_to_end_duration_seconds{category, outcome}`.

    Called at `_notify` completion with `duration = time.time() -
    internalDate_ms / 1000`. Not a context manager because the start
    timestamp is Gmail's `internalDate`, not a Python-side monotonic
    reading — the duration is computed by the caller and passed in.
    """
    email_end_to_end_duration_seconds.labels(
        category=category, outcome=outcome
    ).observe(duration_seconds)
