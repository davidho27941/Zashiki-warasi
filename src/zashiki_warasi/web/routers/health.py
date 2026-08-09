"""GET /healthz — reflects real dependency readiness.

Returns 200 iff DB is reachable AND OAuth token is present + refreshable.
Any failure yields 503 with per-check booleans in the body so
docker/k8s probes and external monitors can act on the specifics.
No network call to Google — the OAuth check is a local inspection of
the cached `Credentials` object.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Response

from zashiki_warasi.core.services import Services
from zashiki_warasi.web.dependencies import get_services

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/healthz")
def healthz(
    response: Response,
    services: Services = Depends(get_services),
) -> dict:
    db_ok = _check_db(services)
    oauth_ok = _check_oauth(services)
    checks = {"db": db_ok, "oauth": oauth_ok}
    if db_ok and oauth_ok:
        return {"status": "healthy", "checks": checks}
    response.status_code = 503
    return {"status": "unhealthy", "checks": checks}


def _check_db(services: Services) -> bool:
    """Cheap `SELECT 1` against the checkpointer pool. Using the same
    pool the app relies on means a pool-exhaustion / config-broken
    condition is observable via the probe, not just an ambient
    "probably fine" signal."""
    try:
        with services.checkpointer_pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except Exception as exc:
        logger.warning(f"/healthz: db check failed: {exc}")
        return False


def _check_oauth(services: Services) -> bool:
    """Truth: credentials object exists AND either not expired OR has
    a refresh_token. No Google API call — that would cost an RTT per
    probe and could hit rate limits under aggressive schedules."""
    creds = getattr(services, "credentials", None)
    if creds is None:
        return False
    expired = bool(getattr(creds, "expired", False))
    refresh_token = getattr(creds, "refresh_token", None)
    return (not expired) or bool(refresh_token)
