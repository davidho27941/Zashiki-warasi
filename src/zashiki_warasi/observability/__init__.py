"""Observability primitives: metrics registry + (later) OTel tracing.

The public API of this package is deliberately narrow:

- `REGISTRY` — the app's `prometheus_client.CollectorRegistry` instance.
  Import this to render `/metrics` output; pass it as `registry=` when
  writing metric declarations that live outside `metrics.py`.
- Every `zashiki_*` metric constant re-exported from `metrics` — call
  sites should `from zashiki_warasi.observability import <metric>`
  rather than reaching into submodules.

Tracing (`configure_tracing`, span helpers) lands in `tracing.py` once
Group 4 begins. Nothing here depends on the OpenTelemetry SDK.
"""

from __future__ import annotations

from zashiki_warasi.observability.metrics import (
    REGISTRY,
    email_end_to_end_duration_seconds,
    external_api_duration_seconds,
    gmail_api_calls_total,
    gmail_api_latency_seconds,
    graph_node_duration_seconds,
    healthz_status,
    llm_call_duration_seconds,
    llm_calls_total,
    llm_latency_seconds,
    oauth_refresh_total,
    oauth_token_expires_in_seconds,
    telegram_send_total,
    tick_conflicts_total,
    tick_duration_seconds,
    tick_messages_processed_total,
    tick_rebaseline_total,
    traces_dropped_total,
)
from zashiki_warasi.observability.spans import (
    APICallCtx,
    LLMCallCtx,
    NodeSpanCtx,
    api_call,
    graph_span,
    llm_call,
    observe_email_end_to_end,
)

__all__ = [
    "REGISTRY",
    "APICallCtx",
    "LLMCallCtx",
    "NodeSpanCtx",
    "api_call",
    "email_end_to_end_duration_seconds",
    "external_api_duration_seconds",
    "gmail_api_calls_total",
    "gmail_api_latency_seconds",
    "graph_node_duration_seconds",
    "graph_span",
    "healthz_status",
    "llm_call",
    "llm_call_duration_seconds",
    "llm_calls_total",
    "llm_latency_seconds",
    "oauth_refresh_total",
    "oauth_token_expires_in_seconds",
    "observe_email_end_to_end",
    "telegram_send_total",
    "tick_conflicts_total",
    "tick_duration_seconds",
    "tick_messages_processed_total",
    "tick_rebaseline_total",
    "traces_dropped_total",
]
