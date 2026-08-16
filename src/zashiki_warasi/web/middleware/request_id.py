"""Request-id middleware — pure ASGI implementation.

Honors `X-Request-ID` from the caller if present, else generates a
12-hex-char UUID slice. Binds into `_REQUEST_ID_CTX` for the duration
of the request so every log record emitted by handlers + downstream
sync/threadpool code carries the same id. Echoes back on the response.

Deliberately implemented as pure ASGI (not `BaseHTTPMiddleware`) so
that middleware + handler run in the same anyio task, which preserves
`contextvars` semantics for downstream OpenTelemetry span context.
`BaseHTTPMiddleware` runs the downstream call in a sub-task via
`anyio.create_task_group()`, and OTel's implicit current-span tracking
(also a `ContextVar`) does not propagate reliably across that task
boundary — the resulting span parents become non-deterministic and the
`POST /poll → zashiki.tick_once → ...` tree required by the
`observability` capability cannot be guaranteed.

See `docs/lessons/2026-08-11-basehttp-middleware-vs-otel-tracing.md`
for the full mechanism write-up (task-local contextvars + anyio task
group + BaseHTTPMiddleware internals) and OpenSpec design D17 for the
change-level decision that motivated the rewrite.

Backward-compat notes:
- Preserves `request.state.request_id = <value>` for handlers that
  read it via the FastAPI Request accessor. `scope["state"]` is
  populated by Starlette's `ServerErrorMiddleware`, which sits outside
  us in the middleware stack, so it always exists by the time we run.
- Response header spelling (`X-Request-ID`), generation length
  (12 hex chars), empty-header treatment (fall through to generate),
  and ContextVar reset-in-finally semantics are identical to the
  pre-v1.1 `BaseHTTPMiddleware` implementation. All existing
  `tests/web/test_request_id.py` cases must continue to pass without
  behavioral change.
"""

from __future__ import annotations

import uuid
from typing import Awaitable, Callable, MutableMapping

from zashiki_warasi.core.logging import _REQUEST_ID_CTX

_HEADER_LOWER = b"x-request-id"
_HEADER_MIXED_BYTES = b"X-Request-ID"
# 12 hex chars = 48 bits of entropy — enough to eyeball as unique across
# a single deploy's log without turning into a wall of characters.
_GENERATED_LEN = 12

# ASGI type shortcuts — kept as plain aliases (no `typing` protocol) so
# the module has zero third-party import cost and stays trivial to test.
_Scope = MutableMapping[str, object]
_Message = MutableMapping[str, object]
_Receive = Callable[[], Awaitable[_Message]]
_Send = Callable[[_Message], Awaitable[None]]


class RequestIdMiddleware:
    """Pure ASGI middleware. Instantiated once at app-startup by
    `FastAPI.add_middleware(RequestIdMiddleware)`, invoked per request
    via `__call__`."""

    def __init__(self, app):
        self.app = app

    async def __call__(
        self, scope: _Scope, receive: _Receive, send: _Send
    ) -> None:
        # Only intercept HTTP requests. WebSocket / lifespan events
        # pass through untouched — we have no reason to attach a
        # request-id to them, and pretending we do would confuse
        # downstream code (lifespan runs once per process, not per
        # "request").
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _resolve_request_id(scope["headers"])  # type: ignore[arg-type]

        # `scope["state"]` is a dict populated by ServerErrorMiddleware
        # (Starlette's outermost middleware) so downstream code can
        # attach per-request state. Setting `request_id` here makes
        # `request.state.request_id` accessible in handlers that
        # request the FastAPI Request object.
        state = scope.setdefault("state", {})
        assert isinstance(state, dict)  # ServerErrorMiddleware guarantees
        state["request_id"] = request_id

        # Bind into the process-wide ContextVar so the log formatter's
        # allowlisted context block picks it up on every log record
        # emitted during handler execution (and any sync work the
        # handler dispatches to the default threadpool that inherits
        # the context).
        token = _REQUEST_ID_CTX.set(request_id)

        # Wrap `send` so the response's initial message gets an
        # X-Request-ID header echoing what we chose. Streaming
        # subsequent chunks pass through untouched.
        async def send_with_header(message: _Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))  # type: ignore[arg-type]
                # Overwrite any prior X-Request-ID header the app may
                # have set — the middleware is the source of truth.
                headers = [
                    (k, v)
                    for (k, v) in headers
                    if k.lower() != _HEADER_LOWER
                ]
                headers.append((_HEADER_MIXED_BYTES, request_id.encode("ascii")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_header)
        finally:
            _REQUEST_ID_CTX.reset(token)


def _resolve_request_id(
    scope_headers: list[tuple[bytes, bytes]],
) -> str:
    """Pull the incoming X-Request-ID from ASGI scope headers (list of
    lowercase-key byte tuples per the ASGI spec). Empty / whitespace-
    only values are treated as absent — falling through to a fresh
    generated id keeps the "empty header does not silently propagate
    a blank id" invariant that the request_id spec requires."""
    for key, value in scope_headers:
        if key.lower() == _HEADER_LOWER:
            candidate = value.decode("ascii", errors="replace").strip()
            if candidate:
                return candidate
            break
    return uuid.uuid4().hex[:_GENERATED_LEN]
