"""Logging bootstrap + context helpers.

Three primitives:

- `configure_logging()` — attach one stream handler + our formatter,
  set root / `zashiki_warasi` levels from `LoggingSettings`, quiet the
  well-known chatty third-party loggers. Idempotent.
- `bind_message_context(logger, message_id=...)` — return a
  `LoggerAdapter` that stamps `message_id` (and any other extras) onto
  every record it produces, so the formatter can render them uniformly.
- `node_trace(log, name)` — context manager that emits DEBUG
  entry/exit lines with `elapsed_ms`, uniformly across every LangGraph
  node in the app.

Stdlib only — no structlog / loguru dep. The formatter shape is
`<ts> LEVEL logger[k=v,k=v]: message`; contextless records omit the
`[...]` block. Swapping to JSON later is a formatter-only change.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Any, Iterator

from zashiki_warasi.core.config import LoggingSettings


# Request-scoped context set by the FastAPI request-id middleware.
# Handlers running inside a request (and any sync work they spawn via
# threadpool that inherits the context) see this value; bootstrap /
# background threads see None and their log records omit request_id.
_REQUEST_ID_CTX: ContextVar[str | None] = ContextVar(
    "zashiki_request_id", default=None
)


# Loggers that would otherwise flood the operator's terminal at INFO
# during a normal Gmail history burst. We pin them at WARNING so
# actual anomalies (auth 401, transport 5xx) still surface.
_CHATTY_THIRD_PARTY_LOGGERS: tuple[str, ...] = (
    "httpx",
    "httpcore",
    "urllib3",
    "google.auth",
    "google_auth_httplib2",
    "openai._base_client",
    "httpx._client",
    # psycopg-pool logs INFO on every pool open/resize/close and DEBUG
    # on every connection lifecycle event. We own our checkpointer
    # pool's INFO lines in `zashiki_warasi.app`, so muting the library
    # keeps our format consistent and avoids double-signaling.
    "psycopg.pool",
)

# Allowlist of `extra` keys the formatter will render (in both the
# text `[k=v]` block and the JSON top-level payload). Anything else on
# the LogRecord's `__dict__` is ignored — prevents accidentally
# leaking arbitrary attributes and keeps the format stable across
# modules.
#
# Order is grep-friendly stability: request_id first (broadest
# scope, one per HTTP request), then trace/span (OTel identity, only
# present when tracing is on), then per-message identifiers set by
# LoggerAdapter extras.
_CONTEXT_FIELDS: tuple[str, ...] = (
    "request_id",
    "trace_id",
    "span_id",
    "message_id",
    "thread_id",
    "expense_id",
)

# Sentinel attribute set on our stream handler so `configure_logging`
# knows a second call is a re-configure, not a first-time attach.
_HANDLER_SENTINEL = "_zashiki_owned"

# Module-scoped flag guarding one-shot install of the OTel-trace-context
# LogRecord factory. Second and subsequent `configure_logging()` calls
# see this True and skip re-installing — chaining N wrappers around
# the same factory would be N times the per-record cost, and there's
# no `uninstall_factory` API to undo it.
_FACTORY_INSTALLED = False

_ZASHIKI_LOGGER_NAME = "zashiki_warasi"


class ContextFormatter(logging.Formatter):
    """One-line formatter that appends allowlisted context fields.

    Format (contextless):
        2026-07-28T09:12:34+0000 INFO zashiki_warasi.gmail.poller: tick complete: 0 new

    Format (with context):
        2026-07-28T09:12:34+0000 INFO zashiki_warasi.agents.email_agent[message_id=abc123]: classified as 消費支出
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s %(levelname)s %(name)s%(zashiki_context)s: %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )

    def format(self, record: logging.LogRecord) -> str:
        # Merge two sources of context into the [k=v,...] block:
        # (1) request_id from the module-scoped ContextVar the FastAPI
        #     middleware sets — flows into every sync/async call inside
        #     a request without any per-call plumbing.
        # (2) message_id / thread_id / expense_id from LoggerAdapter
        #     `extra=` — set by `bind_message_context` for per-message
        #     code paths.
        # Missing values render nothing (no `request_id=None`). Field
        # order follows `_CONTEXT_FIELDS` declaration order so
        # grep-friendly log output stays stable across releases.
        pairs: list[str] = []
        for key in _CONTEXT_FIELDS:
            if key == "request_id":
                value = _REQUEST_ID_CTX.get()
            else:
                value = record.__dict__.get(key)
            if value is None:
                continue
            pairs.append(f"{key}={value}")
        record.zashiki_context = f"[{','.join(pairs)}]" if pairs else ""
        return super().format(record)


class JsonContextFormatter(logging.Formatter):
    """One-JSON-object-per-record formatter (NDJSON / JSON Lines).

    Contract:
    - `timestamp`: ISO-8601 UTC with millisecond precision, Z suffix
    - `level`: uppercase name (`DEBUG` / `INFO` / ...)
    - `logger`: dotted Python logger name
    - `message`: post-`%`-substitution message string
    - Every context field in `_CONTEXT_FIELDS` that has a value
      becomes a top-level key (not nested — log-search tools filter
      without knowing the structure). Absent fields OMITTED entirely
      (never emitted as `null` / `""` / `"None"`).
    - On `exc_info`: `traceback` string field + `exception` object
      `{type, message}`.

    Serialization uses `ensure_ascii=False` — this codebase's log
    messages are frequently Chinese (`classified as 消費支出`,
    category names, notification text). Default `ensure_ascii=True`
    would render them as `消費支出` — technically valid JSON,
    operationally a disaster (grep-by-substring breaks, human-
    readable dashboards break).

    Serialization uses `default=str` — `extra=` may carry `datetime`,
    `Decimal`, `UUID`, `Path`, custom objects. Without a fallback,
    `json.dumps` raises `TypeError` inside the logger call, which
    propagates and can crash whatever code emitted the log line.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": _iso_utc_timestamp(record.created),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in _CONTEXT_FIELDS:
            if key == "request_id":
                value: Any = _REQUEST_ID_CTX.get()
            else:
                value = record.__dict__.get(key)
            # Falsy skip: None, "", 0 all excluded — the contract is
            # "field appears only when meaningful", matching text
            # formatter's `if value is None: continue` intent.
            if value:
                payload[key] = value
        if record.exc_info:
            payload["traceback"] = self.formatException(record.exc_info)
            exc_type, exc_value, _ = record.exc_info
            payload["exception"] = {
                "type": exc_type.__name__ if exc_type else "",
                "message": str(exc_value) if exc_value else "",
            }
        return json.dumps(payload, ensure_ascii=False, default=str)


def _iso_utc_timestamp(unix_ts: float) -> str:
    """Format a Unix timestamp as ISO-8601 UTC with milliseconds + Z.

    Example: `2026-08-15T09:12:34.567Z`
    """
    dt = datetime.fromtimestamp(unix_ts, tz=timezone.utc)
    ms = dt.microsecond // 1000
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ms:03d}Z"


class _MessageContextAdapter(logging.LoggerAdapter):
    """LoggerAdapter that merges its `extra` into each record.

    Subclass so callers can distinguish "wrapped for message context"
    from a plain LoggerAdapter in tests / assertions.
    """

    def process(
        self, msg: str, kwargs: dict[str, Any]
    ) -> tuple[str, dict[str, Any]]:
        # Merge our stored extras into any per-call `extra` the caller
        # provided (per-call wins on conflict so short-lived overrides
        # are respected).
        merged_extra = {**(self.extra or {}), **kwargs.get("extra", {})}
        kwargs["extra"] = merged_extra
        return msg, kwargs


def bind_message_context(
    logger: logging.Logger | logging.LoggerAdapter,
    *,
    message_id: str,
    **extra: Any,
) -> _MessageContextAdapter:
    """Wrap `logger` so every record it produces carries `message_id`.

    Pass the returned adapter into helpers that emit per-message logs
    (nodes, notifiers, persistence writers) so `grep message_id=<id>`
    on the log output surfaces the full lifecycle across modules.

    Chaining is safe: passing an existing adapter as `logger` merges
    its extras with the new ones (new-value wins on collision).
    """
    if isinstance(logger, logging.LoggerAdapter):
        base_logger = logger.logger
        base_extra = dict(logger.extra or {})
    else:
        base_logger = logger
        base_extra = {}
    base_extra["message_id"] = message_id
    base_extra.update({k: v for k, v in extra.items() if v is not None})
    return _MessageContextAdapter(base_logger, base_extra)


@contextmanager
def node_trace(
    log: logging.Logger | logging.LoggerAdapter, name: str
) -> Iterator[None]:
    """DEBUG-level entry/exit trace + OpenTelemetry span around a
    LangGraph node body.

    Usage inside a node method:

        with node_trace(log, "analyze"):
            ...

    Emits `node=<name> enter` on entry and, on exit:

    - normal return: `node=<name> exit elapsed_ms=<int>`
    - exception:     `node=<name> exit_error elapsed_ms=<int> exc=<type>`
      (then re-raises the original exception unchanged)

    In parallel, opens an OpenTelemetry span named `zashiki.node.<name>`.
    When `log` is a `LoggerAdapter` carrying `message_id` in its `extra`,
    it's attached as `zashiki.message_id` span attribute so the span
    grep-joins with the log stream. On exception the span is marked
    ERROR and the exception recorded on it; the exception itself
    propagates unchanged.

    `elapsed_ms` uses `time.monotonic` — safe against wall-clock skew.
    Overhead is a few microseconds per node call under a NoOp tracer
    (OTel disabled) and ~tens of microseconds under a real tracer —
    insignificant next to the LLM / DB / HTTP work inside every node.
    """
    # OTel API is always installed (opentelemetry-api is a v1.1 dep);
    # under OTEL_ENABLED=0 the tracer_provider is NoOp and
    # start_as_current_span produces a NoOp span with near-zero cost.
    from opentelemetry import trace

    tracer = trace.get_tracer("zashiki_warasi.node")

    log.debug(f"node={name} enter")
    started = time.monotonic()
    with tracer.start_as_current_span(f"zashiki.node.{name}") as span:
        _attach_context_attributes(span, log)
        try:
            yield
        except BaseException as exc:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.debug(
                f"node={name} exit_error elapsed_ms={elapsed_ms} "
                f"exc={type(exc).__name__}"
            )
            _mark_span_error(span, exc)
            raise
        else:
            elapsed_ms = int((time.monotonic() - started) * 1000)
            log.debug(f"node={name} exit elapsed_ms={elapsed_ms}")


def _attach_context_attributes(
    span, log: logging.Logger | logging.LoggerAdapter
) -> None:
    """Copy well-known context (`request_id`, `message_id`) from the
    logger / contextvar into the span so operators grepping either
    signal can pivot to the other. No-op on NoOp spans."""
    request_id = _REQUEST_ID_CTX.get()
    if request_id:
        span.set_attribute("zashiki.request_id", request_id)
    if isinstance(log, logging.LoggerAdapter):
        message_id = (log.extra or {}).get("message_id")
        if message_id:
            span.set_attribute("zashiki.message_id", message_id)


def _mark_span_error(span, exc: BaseException) -> None:
    """Set span status to ERROR + record the exception. Guarded so a
    NoOp span (OTEL_ENABLED=0) does nothing."""
    from opentelemetry.trace import Status, StatusCode

    span.set_status(Status(StatusCode.ERROR, str(exc)))
    # record_exception is safe on NoOp spans (no-op) and adds an
    # exception event on real spans.
    span.record_exception(exc)


def configure_logging(settings: LoggingSettings | None = None) -> None:
    """Bootstrap logging for the app process.

    Order matters:
      1. Resolve settings (fail-fast on invalid level / format via
         `LoggingSettings`).
      2. Install the OTel trace-context LogRecord factory ONCE per
         process (idempotent, uses `_FACTORY_INSTALLED` guard).
      3. Attach a single StreamHandler(sys.stderr) to the root logger
         if not already attached.
      4. Set the handler's formatter based on `settings.format`
         (`ContextFormatter` for `text`, `JsonContextFormatter` for
         `json`). Runs on every call so re-configure can flip format.
      5. Set root level.
      6. Set `zashiki_warasi` tree level if operator overrode.
      7. Pin chatty third-party loggers to WARNING.

    Safe to call more than once. Re-entry updates levels and formatter
    without stacking handlers (duplicated output lines) or factories
    (chained N-times slowdown).
    """
    settings = settings or LoggingSettings()

    _install_trace_context_factory()

    root = logging.getLogger()
    if not _has_our_handler(root):
        handler = logging.StreamHandler(sys.stderr)
        setattr(handler, _HANDLER_SENTINEL, True)
        root.addHandler(handler)

    formatter = _build_formatter(settings.format)
    for h in root.handlers:
        if getattr(h, _HANDLER_SENTINEL, False):
            h.setFormatter(formatter)

    root.setLevel(_level_int(settings.level))

    zashiki_logger = logging.getLogger(_ZASHIKI_LOGGER_NAME)
    if settings.level_zashiki is None:
        # Clear any prior override so we truly inherit from root on
        # re-configure. NOTSET (=0) makes effective level fall back to
        # the parent chain.
        zashiki_logger.setLevel(logging.NOTSET)
    else:
        zashiki_logger.setLevel(_level_int(settings.level_zashiki))

    for name in _CHATTY_THIRD_PARTY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _build_formatter(fmt: str) -> logging.Formatter:
    """Map the validated `LoggingSettings.format` value to the actual
    Formatter instance. `LoggingSettings` validation guarantees the
    value is `text` or `json` (lowercased), so the else-branch is
    defensive-only."""
    if fmt == "json":
        return JsonContextFormatter()
    return ContextFormatter()


def _install_trace_context_factory() -> None:
    """Chain-wrap `logging.getLogRecordFactory()` exactly once per
    process so every emitted LogRecord carries `trace_id` and
    `span_id` when an OTel span context is active.

    - `_FACTORY_INSTALLED` guard: idempotent. `configure_logging()`
      may be called multiple times (tests, lifespan restarts); we
      install our wrapper only on the first call. Chaining N times
      would incur N-times the per-record cost forever after.
    - Chain-wrap (not replace) the existing factory so anything a
      test harness / third-party library installed before us keeps
      working.
    - Fully guarded: if `opentelemetry.trace` cannot be imported or
      the SDK misbehaves, the wrapper degrades to a no-op — a log
      emission must NEVER crash because of telemetry.
    - Uses lowercase-hex formatting (`032x` for trace_id, `016x`
      for span_id) matching W3C traceparent + Grafana Tempo query
      conventions.
    """
    global _FACTORY_INSTALLED
    if _FACTORY_INSTALLED:
        return
    _FACTORY_INSTALLED = True

    prior_factory = logging.getLogRecordFactory()

    def _trace_context_factory(
        *args: Any, **kwargs: Any
    ) -> logging.LogRecord:
        record = prior_factory(*args, **kwargs)
        try:
            from opentelemetry import trace

            span = trace.get_current_span()
            ctx = span.get_span_context() if span is not None else None
            if ctx is not None and ctx.is_valid:
                record.trace_id = format(ctx.trace_id, "032x")
                record.span_id = format(ctx.span_id, "016x")
        except Exception:
            # OTel not installed, or SDK internal error — telemetry
            # must never propagate exceptions into the log call path.
            pass
        return record

    logging.setLogRecordFactory(_trace_context_factory)


def _has_our_handler(logger: logging.Logger) -> bool:
    return any(getattr(h, _HANDLER_SENTINEL, False) for h in logger.handlers)


def _level_int(name: str) -> int:
    # LoggingSettings has already validated `name` — this is just the
    # stdlib lookup. Kept as a helper for readability at call sites.
    return logging.getLevelNamesMapping()[name]
