"""OpenTelemetry bootstrap for the runtime.

Public entry point: `configure_tracing(settings, logger=None)`. Called
once at process startup (from the FastAPI lifespan). All OTel SDK
modules are lazy-imported behind the enabled branch so operators who
don't opt in pay zero import cost.

Behavior contract (see `openspec/specs/observability/spec.md`):

1. `OTEL_ENABLED != "1"` — completely no-op. No SDK import, no
   TracerProvider set, no OTLP connection attempted. Downstream code
   that calls `tracer.start_as_current_span(...)` gets the SDK's
   default NoOp tracer.

2. `OTEL_ENABLED == "1"`:
   - Verify `OTEL_RESOURCE_ATTRIBUTES` does not contain secret-shaped
     values (D22). If it does, exit non-zero at startup — resource
     attributes are attached to every exported span and a leaked API
     key would persist forever in Tempo.
   - Verify `WEB_CONCURRENCY <= 1` (D18). Multi-worker mode fragments
     the process-local Prometheus registry across workers and would
     also produce split trace lineage. Refuse to boot.
   - Build a `Resource` with `service.name`, `service.version`,
     `service.instance.id`, and any merged `OTEL_RESOURCE_ATTRIBUTES`.
   - Build a `TracerProvider(sampler=..., resource=...)` and register
     it with the SDK.
   - Install a `BatchSpanProcessor(OTLPSpanExporter(endpoint=...))`
     wrapped in `_RateLimitedBatchSpanProcessor` — the wrapper
     rate-limits exporter-error WARNINGs to one per minute per
     category and increments `zashiki_traces_dropped_total{reason=...}`
     so operators can see silent trace loss without log-spelunking.
   - Disable LangGraph / LangChain internal OTel emission by setting
     `LANGCHAIN_TRACING_V2` / `LANGSMITH_TRACING` to `false` BEFORE
     importing those libraries elsewhere in the app (D23). If left on,
     they'd emit their own `langchain.*` / `langgraph.*` spans that
     duplicate our manual `zashiki.node.*` wrapping.
   - Auto-instrument httpx and psycopg — FastAPI is instrumented
     separately by the app factory (needs the FastAPI app object,
     which we don't own here).

Exporter failures NEVER block a request. `BatchSpanProcessor` runs
export in a background thread; the wrapper here catches
`export_failed` / `queue_full` / `shutdown` outcomes and turns them
into telemetry-only signals.
"""

from __future__ import annotations

import base64
import logging
import os
import re
import sys
import uuid

from zashiki_warasi.core.config import ObservabilitySettings

logger = logging.getLogger(__name__)


# --- Public entry point ---------------------------------------------


def configure_tracing(
    settings: ObservabilitySettings | None = None,
    *,
    log: logging.Logger | None = None,
) -> None:
    """Bootstrap OpenTelemetry per the settings. Idempotent — a second
    call with the same settings is a no-op (the SDK's tracer_provider
    override protects us).
    """
    settings = settings or ObservabilitySettings()
    log = log or logger

    check_web_concurrency(log)

    if not settings.otel_enabled:
        log.debug(
            "OTEL_ENABLED=%s → tracing disabled (SDK not initialized)",
            settings.otel_enabled,
        )
        return

    _guard_resource_attributes_secrets(
        settings.otel_resource_attributes, log
    )
    _disable_langchain_internal_tracing()

    # Lazy imports — SDK cost is only paid on the enabled path.
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.sdk.trace import TracerProvider

    resource = _build_resource(settings)
    sampler = _build_sampler(settings)
    provider = TracerProvider(sampler=sampler, resource=resource)

    exporter = OTLPSpanExporter(
        endpoint=settings.otel_exporter_otlp_endpoint,
    )
    # Lazy-import the BSP submodule — its top-level `from
    # opentelemetry.sdk.trace.export import BatchSpanProcessor` is what
    # triggers the SDK import chain, and we only want that on the
    # enabled path (design D24).
    from zashiki_warasi.observability._rate_limited_bsp import (
        RateLimitedBSP,
    )

    processor = RateLimitedBSP(exporter, log=log)
    provider.add_span_processor(processor)
    trace.set_tracer_provider(provider)

    _instrument_libraries(log)

    log.info(
        "OTel tracing enabled: service=%s endpoint=%s sampler=%s ratio=%s",
        settings.otel_service_name,
        settings.otel_exporter_otlp_endpoint,
        settings.otel_traces_sampler,
        settings.otel_traces_sampler_arg,
    )


# --- WEB_CONCURRENCY guard (D18 / observability spec) ---------------


def check_web_concurrency(log: logging.Logger | None = None) -> None:
    """Exit non-zero if uvicorn is asked to run > 1 worker.

    Prometheus `CollectorRegistry` is process-local. Multi-worker mode
    fragments counters across workers, breaking scrape coherence. Rather
    than support `PROMETHEUS_MULTIPROC_DIR` mode for a scale-out we do
    not need (replicas via k8s already handle it), fail-fast at bootstrap
    with a diagnostic message.

    Called from `configure_tracing()` because it also runs at startup —
    but is safe to invoke standalone (no OTel imports required).
    """
    log = log or logger
    raw = os.environ.get("WEB_CONCURRENCY", "").strip()
    if not raw:
        return
    try:
        value = int(raw)
    except ValueError:
        # An unparseable value is a config bug; refuse to boot rather
        # than let uvicorn later crash with a less helpful error.
        log.critical(
            "WEB_CONCURRENCY=%r is not an integer. Unset it or set to '1'.",
            raw,
        )
        sys.exit(1)
    if value > 1:
        log.critical(
            "WEB_CONCURRENCY=%d is unsupported: prometheus_client "
            "registry is process-local and multi-worker mode fragments "
            "counters across workers, breaking scraper contract. Use "
            "replicaCount for horizontal scale instead. See "
            "docs/lessons/2026-08-12-prometheus-multi-worker.md",
            value,
        )
        sys.exit(1)


# --- Secret guard (D22 / observability spec) ------------------------


# Well-known secret-token prefixes. Not exhaustive — the goal is to
# catch top-5 embarrassing mistakes, not to be a full secret scanner.
_SECRET_PREFIXES: tuple[str, ...] = (
    "sk-",       # OpenAI / Anthropic legacy
    "ghp_",      # GitHub PAT (classic)
    "github_pat_",  # GitHub PAT (fine-grained)
    "AIza",      # Google API key
    "xoxb-",     # Slack bot token
    "xoxp-",     # Slack user token
    "xoxa-",     # Slack app token
    "xoxs-",     # Slack legacy token
    "AKIA",      # AWS access key id
)

# Base64-looking shape: purely [A-Za-z0-9+/=] AND length >= 32.
# Catches many raw tokens that don't have a recognizable prefix.
_BASE64_SHAPE = re.compile(r"^[A-Za-z0-9+/=_-]{32,}$")


def _guard_resource_attributes_secrets(
    raw: str, log: logging.Logger
) -> None:
    """Refuse to boot when OTEL_RESOURCE_ATTRIBUTES looks like it
    carries a secret. Logs the OFFENDING KEY + MATCHED PATTERN, never
    the value itself — the diagnostic line must not become a second
    leak vector.
    """
    if not raw:
        return
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        key = key.strip()
        value = value.strip()
        matched = _match_secret_shape(value)
        if matched is None:
            continue
        log.critical(
            "OTEL_RESOURCE_ATTRIBUTES key %r matched secret pattern %r "
            "and would be attached to every exported span. Refusing to "
            "boot. Remove the offending key/value from the env var.",
            key,
            matched,
        )
        sys.exit(1)


def _match_secret_shape(value: str) -> str | None:
    """Return the description of the first match, or None."""
    for prefix in _SECRET_PREFIXES:
        if value.startswith(prefix):
            return f"prefix={prefix!r}"
    if _BASE64_SHAPE.match(value):
        # Also accept classical base64 padding
        try:
            base64.b64decode(value + "==", validate=False)
        except Exception:
            pass
        return "base64-like (>=32 chars)"
    return None


# --- LangGraph / LangChain double-emit prevention (D23) -------------


def _disable_langchain_internal_tracing() -> None:
    """Set env flags that tell LangChain / LangSmith / LangGraph to
    NOT emit their own OTel spans. We wrap those code paths with our
    own `zashiki.node.*` spans; letting the libraries also emit would
    double the span tree with no unique information.

    Use `setdefault` so operator-provided overrides win (someone might
    genuinely want LangSmith on during debugging).
    """
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "false")
    os.environ.setdefault("LANGSMITH_TRACING", "false")


# --- Resource + sampler builders ------------------------------------


def _build_resource(settings: ObservabilitySettings):
    """Build the OTel Resource attached to every span."""
    from importlib.metadata import PackageNotFoundError, version

    from opentelemetry.sdk.resources import Resource

    try:
        service_version = version("zashiki-warasi")
    except PackageNotFoundError:
        service_version = "unknown"

    # `HOSTNAME` is auto-set by both docker and kubernetes to the
    # container / pod name, which is what operators see in
    # `kubectl get pods` / `docker ps`. Falls back to a uuid so the
    # value stays stable per process even in exotic environments.
    instance_id = os.environ.get("HOSTNAME") or uuid.uuid4().hex

    base_attrs = {
        "service.name": settings.otel_service_name,
        "service.version": service_version,
        "service.instance.id": instance_id,
    }
    extra = _parse_resource_attributes(settings.otel_resource_attributes)
    return Resource.create({**base_attrs, **extra})


def _parse_resource_attributes(raw: str) -> dict[str, str]:
    """Parse the OTel-standard comma-separated `k=v,k=v` form. Empty
    tokens are skipped. Values are trimmed but otherwise passed through
    verbatim (spaces inside values survive)."""
    if not raw:
        return {}
    out: dict[str, str] = {}
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        out[key.strip()] = value.strip()
    return out


def _build_sampler(settings: ObservabilitySettings):
    """Map the string sampler name to an SDK sampler instance.

    We support the two OTel-spec samplers we actually need:
    `parentbased_traceidratio` (default) and `traceidratio`.
    """
    from opentelemetry.sdk.trace.sampling import (
        ALWAYS_OFF,
        ALWAYS_ON,
        ParentBased,
        TraceIdRatioBased,
    )

    name = settings.otel_traces_sampler.lower()
    arg = settings.otel_traces_sampler_arg
    if name == "always_on":
        return ALWAYS_ON
    if name == "always_off":
        return ALWAYS_OFF
    if name == "traceidratio":
        return TraceIdRatioBased(arg)
    if name == "parentbased_traceidratio":
        return ParentBased(root=TraceIdRatioBased(arg))
    # Unknown sampler name — behave like the OTel SDK would with a
    # missing env var, and pick a reasonable default rather than crash.
    logger.warning(
        "Unknown OTEL_TRACES_SAMPLER=%r; falling back to "
        "parentbased_traceidratio",
        name,
    )
    return ParentBased(root=TraceIdRatioBased(arg))


# NOTE: `RateLimitedBSP` lives in `observability/_rate_limited_bsp.py`
# (top-level class in a dedicated submodule, design D24). It is imported
# lazily inside `configure_tracing()` above, preserving the OTEL_ENABLED=0
# zero-SDK-import contract while giving up the code-smell of a factory
# with a class defined inline.


# --- Library instrumentation ----------------------------------------


def _instrument_libraries(log: logging.Logger) -> None:
    """Turn on OTel instrumentation for the framework layer.

    FastAPI instrumentation happens in the app factory (needs the app
    object). Here we cover the transport libraries:

    - httpx: covers Gmail API (google-api-client uses httplib2, NOT
      httpx, so gmail spans still come from our manual `record_call`
      wrapper — but langchain-openai uses httpx, and any future
      direct-httpx call gets automatically traced).
    - psycopg: covers the checkpointer pool + advisory-lock queries.
      Compat verified against psycopg 3.2.13 in task 1.3.
    """
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    try:
        HTTPXClientInstrumentor().instrument()
    except Exception:
        log.exception(
            "Failed to instrument httpx; continuing without HTTP client spans"
        )

    try:
        PsycopgInstrumentor().instrument()
    except Exception:
        log.exception(
            "Failed to instrument psycopg; continuing without DB spans"
        )
