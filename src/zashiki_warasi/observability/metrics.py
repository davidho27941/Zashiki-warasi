"""Prometheus metric registry + business-metric contract.

The metric names, types, and label sets defined here are the CONTRACT
that Grafana dashboards + Prometheus alerts consume. Renaming a metric
or altering its labelset is a breaking change to those downstream
artifacts — treat the table in `openspec/specs/observability/spec.md`
as the source of truth and update in lockstep.

Design notes worth carrying at the top of the file (fuller rationale
in `openspec/changes/add-observability-stack/design.md`):

- We use a dedicated `REGISTRY = CollectorRegistry()` instead of the
  library-global `prometheus_client.REGISTRY`. This lets tests spawn a
  fresh registry per case without leaking state, and it decouples the
  app's exposition from anything else in the process that might touch
  the default registry.
- Default process / GC / platform collectors are attached by
  CONSTRUCTING fresh instances against our registry
  (`ProcessCollector(registry=REGISTRY)` etc.) — NOT by registering the
  pre-bound `PROCESS_COLLECTOR` singleton, which is already attached to
  the default global registry and would raise `Duplicated timeseries`
  on our registry.
- Every metric declaration passes through `_assert_no_message_id_label`
  as a compile-time-ish guard against unbounded cardinality. Adding
  `message_id` (or any per-message / per-user identifier) as a label
  would make Prometheus's memory grow linearly with lifetime message
  volume. Spans and log fields are the right places for that data.
- There is intentionally NO generic `zashiki_http_requests_total`
  counter. Each concern with real diagnostic value has a dedicated
  business metric (tick / gmail / llm / telegram / oauth / healthz);
  adding a generic HTTP counter would double-count the /poll flow
  without adding unique information.
"""

from __future__ import annotations

from prometheus_client import (
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
)
from prometheus_client.gc_collector import GCCollector
from prometheus_client.platform_collector import PlatformCollector
from prometheus_client.process_collector import ProcessCollector

# Compile-time cardinality guardrails. Adding entries here is the way
# to grow the forbidden-label list without touching every declaration.
_FORBIDDEN_LABEL_NAMES: frozenset[str] = frozenset({
    "message_id",
    "email",
    "email_address",
    "user_id",
})


def _assert_no_forbidden_labels(name: str, labelnames: tuple[str, ...]) -> None:
    """Raise at declaration time if `labelnames` contains a name known
    to blow up Prometheus cardinality. The check runs during module
    import (which is where `Counter(...)`, `Gauge(...)` etc. execute),
    so a mistake shows up as an ImportError long before deploy — and
    is covered by a regression test in tests/observability/test_metrics.py.
    """
    forbidden = set(labelnames) & _FORBIDDEN_LABEL_NAMES
    if forbidden:
        raise ValueError(
            f"metric {name!r} declared with forbidden high-cardinality "
            f"label(s) {sorted(forbidden)}. Move that value onto a span "
            f"attribute or a log context field, NOT a metric label."
        )


REGISTRY: CollectorRegistry = CollectorRegistry()

# Default collectors: process_* / python_gc_* / python_info families.
# See file-top docstring for why we construct fresh instances instead
# of registering the pre-bound singletons. The returned collector
# objects are attached to REGISTRY as a side effect of construction;
# we drop the references (they're kept alive via the registry).
ProcessCollector(registry=REGISTRY)
GCCollector(registry=REGISTRY)
PlatformCollector(registry=REGISTRY)


# --- Metric family constructor with the guard applied ---

def _counter(name: str, doc: str, labelnames: tuple[str, ...] = ()) -> Counter:
    _assert_no_forbidden_labels(name, labelnames)
    return Counter(name, doc, labelnames=labelnames, registry=REGISTRY)


def _gauge(name: str, doc: str, labelnames: tuple[str, ...] = ()) -> Gauge:
    _assert_no_forbidden_labels(name, labelnames)
    return Gauge(name, doc, labelnames=labelnames, registry=REGISTRY)


def _histogram(
    name: str, doc: str, labelnames: tuple[str, ...] = ()
) -> Histogram:
    _assert_no_forbidden_labels(name, labelnames)
    return Histogram(name, doc, labelnames=labelnames, registry=REGISTRY)


# --- Business metrics contract (see specs/observability/spec.md) ---

# Tick lifecycle. `outcome` is `success` or `error` — 409-conflict
# responses never enter the tick body (they short-circuit at the
# advisory-lock acquisition), so `conflict` is NOT a valid outcome.
# Conflicts are counted separately by `tick_conflicts_total`.
tick_duration_seconds: Histogram = _histogram(
    "zashiki_tick_duration_seconds",
    "Wall-clock time of one POST /poll handler invocation that acquired "
    "the single-flight lock.",
    labelnames=("outcome",),
)
tick_messages_processed_total: Counter = _counter(
    "zashiki_tick_messages_processed_total",
    "Cumulative count of Gmail messages successfully processed by tick_once.",
)
tick_conflicts_total: Counter = _counter(
    "zashiki_tick_conflicts_total",
    "Cumulative count of POST /poll calls that returned 409 tick_in_flight "
    "because the advisory lock was already held.",
)
tick_rebaseline_total: Counter = _counter(
    "zashiki_tick_rebaseline_total",
    "Cumulative count of ticks that triggered a Gmail history rebaseline "
    "(HistoryExpiredError).",
)

# Gmail API surface. `operation` is one of the Gmail method families
# we actually call (history / get / modify); `outcome` is success or error.
gmail_api_calls_total: Counter = _counter(
    "zashiki_gmail_api_calls_total",
    "Cumulative Gmail API calls issued.",
    labelnames=("operation", "outcome"),
)
gmail_api_latency_seconds: Histogram = _histogram(
    "zashiki_gmail_api_latency_seconds",
    "Latency of Gmail API calls.",
    labelnames=("operation",),
)

# LLM adapter. `node` is the calling LangGraph node
# (analyze / expense_extract / expense_resolve); `outcome` is success or error.
llm_calls_total: Counter = _counter(
    "zashiki_llm_calls_total",
    "Cumulative LLM invocations.",
    labelnames=("node", "outcome"),
)
llm_latency_seconds: Histogram = _histogram(
    "zashiki_llm_latency_seconds",
    "Latency of LLM invocations.",
    labelnames=("node",),
)

# Notification sink.
telegram_send_total: Counter = _counter(
    "zashiki_telegram_send_total",
    "Cumulative Telegram alert dispatches.",
    labelnames=("outcome",),
)

# OAuth refresh path. `outcome` reflects the token-endpoint call
# success or failure; it does NOT reflect whether the refresh was
# even attempted.
oauth_refresh_total: Counter = _counter(
    "zashiki_oauth_refresh_total",
    "Cumulative OAuth token refresh attempts.",
    labelnames=("outcome",),
)

# Cached-credential expiry countdown, updated on the /healthz code
# path (see healthz handler). Negative values are valid — they mean
# the token has expired but a refresh has not yet run. `0.0` when
# no cached credential is present.
oauth_token_expires_in_seconds: Gauge = _gauge(
    "zashiki_oauth_token_expires_in_seconds",
    "Seconds until the currently-cached OAuth Credentials.expiry. "
    "Negative when the token has expired but refresh has not run. "
    "0.0 when no cached credential is present. Enables pre-emptive "
    "alerts before oauth_refresh_total{outcome=error} would fire.",
)

# Healthz snapshot. `1` when both DB and OAuth checks pass in the
# most recent /healthz evaluation; `0` when either fails. Set by
# the /healthz handler on the same code path that already computes
# the checks — no extra probe.
healthz_status: Gauge = _gauge(
    "zashiki_healthz_status",
    "Result of the most recent /healthz evaluation: 1 = both DB and "
    "OAuth checks passed, 0 = at least one failed.",
)

# OTel BatchSpanProcessor drop counter. `reason` is `queue_full`,
# `export_failed`, or `shutdown`. Zero when OTEL_ENABLED=0 (no
# tracing → no drops). Enables "collector has been down for X → Y
# traces silently gone" surfacing without log spelunking.
traces_dropped_total: Counter = _counter(
    "zashiki_traces_dropped_total",
    "Cumulative count of spans the OTel BatchSpanProcessor could not "
    "export. Non-zero indicates trace data loss.",
    labelnames=("reason",),
)


# --- Introspection helpers ---

def all_metric_names() -> list[str]:
    """Return the sorted list of metric family names currently exposed
    by our REGISTRY on scrape. Iterates the same `REGISTRY.collect()`
    that `generate_latest()` walks, so what this returns is exactly
    what a scraper would see (minus label variants).

    On non-Linux hosts (e.g. macOS dev laptops), `ProcessCollector`
    yields nothing because `/proc` does not exist — tests that assert
    on `process_cpu_seconds_total` must skip / xfail off-Linux.
    """
    return sorted({metric.name for metric in REGISTRY.collect()})
