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

import logging
import sys
import time
from contextlib import contextmanager
from contextvars import ContextVar
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

# Allowlist of `extra` keys the formatter will render inside the
# `[k=v]` block. Anything else on the LogRecord's `__dict__` is
# ignored — prevents accidentally leaking arbitrary attributes and
# keeps the format stable across modules.
_CONTEXT_FIELDS: tuple[str, ...] = (
    "request_id",
    "message_id",
    "thread_id",
    "expense_id",
)

# Sentinel attribute set on our stream handler so `configure_logging`
# knows a second call is a re-configure, not a first-time attach.
_HANDLER_SENTINEL = "_zashiki_owned"

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

    Order matters (see design.md D6):
      1. Resolve settings (fail-fast on invalid level via LoggingSettings).
      2. Attach a single StreamHandler(sys.stderr) with our formatter
         to the root logger — if already attached, skip.
      3. Set root level.
      4. Set `zashiki_warasi` level if the operator gave an override,
         otherwise clear it so the root level rules.
      5. Pin chatty third-party loggers to WARNING.

    Safe to call more than once. Re-entry updates levels without
    stacking handlers (which would cause duplicated output lines).
    """
    settings = settings or LoggingSettings()

    root = logging.getLogger()
    if not _has_our_handler(root):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(ContextFormatter())
        setattr(handler, _HANDLER_SENTINEL, True)
        root.addHandler(handler)

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


def _has_our_handler(logger: logging.Logger) -> bool:
    return any(getattr(h, _HANDLER_SENTINEL, False) for h in logger.handlers)


def _level_int(name: str) -> int:
    # LoggingSettings has already validated `name` — this is just the
    # stdlib lookup. Kept as a helper for readability at call sites.
    return logging.getLevelNamesMapping()[name]
