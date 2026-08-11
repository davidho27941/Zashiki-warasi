"""Request-id middleware.

Honors `X-Request-ID` from the caller if present, else generates a
12-hex-char UUID slice. Binds into `_REQUEST_ID_CTX` for the duration
of the request so every log record emitted by handlers + downstream
sync/threadpool code carries the same id. Echoes back on the response.
"""

from __future__ import annotations

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from zashiki_warasi.core.logging import _REQUEST_ID_CTX

_HEADER = "X-Request-ID"
# 12 hex chars = 48 bits of entropy — enough to eyeball as unique across
# a single deploy's log without turning into a wall of characters.
_GENERATED_LEN = 12


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        incoming = request.headers.get(_HEADER, "").strip()
        request_id = incoming or uuid.uuid4().hex[:_GENERATED_LEN]

        request.state.request_id = request_id
        token = _REQUEST_ID_CTX.set(request_id)
        try:
            response = await call_next(request)
        finally:
            _REQUEST_ID_CTX.reset(token)
        response.headers[_HEADER] = request_id
        return response
