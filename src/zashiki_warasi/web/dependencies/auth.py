"""`require_api_key` FastAPI dependency.

When `HTTP_API_KEY` is set on `services.http_settings`, `/poll` and
`/reauth` require an `X-API-Key` request header whose value matches.
When unset (None or empty string), the dependency is a no-op — the
loopback-only bind default makes an unauthenticated stance safe.
The bootstrap-time cross-field validator in `HttpSettings` prevents
the dangerous combination `bind != 127.0.0.1 AND api_key unset` from
ever reaching this dependency.
"""

from __future__ import annotations

from fastapi import Depends, Header, HTTPException

from zashiki_warasi.core.services import Services
from zashiki_warasi.web.dependencies.services import get_services

_HEADER_NAME = "X-API-Key"


def require_api_key(
    services: Services = Depends(get_services),
    x_api_key: str | None = Header(default=None, alias=_HEADER_NAME),
) -> None:
    """Raise 401 iff `HTTP_API_KEY` is set and the header doesn't match.

    Returns None on success; FastAPI ignores the return value of a
    dependency used purely for its side-effect (auth check).
    """
    configured = services.http_settings.api_key
    if not configured:
        return  # auth disabled — safe under loopback-only bind
    if x_api_key is None or x_api_key != configured:
        raise HTTPException(
            status_code=401,
            detail="invalid_or_missing_api_key",
        )
