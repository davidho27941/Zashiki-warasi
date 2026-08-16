"""GET /metrics — Prometheus text-format scrape endpoint.

Unconditionally registered (there is no `METRICS_ENABLED`-style env
toggle — see design D7). The endpoint exposes counters + gauges +
histograms + the default process/GC/platform families attached to
`REGISTRY` at import time (see `zashiki_warasi.observability.metrics`).

Access control is a network-layer concern: run behind a ClusterIP
Service (k3s) or bind loopback-only on the host (compose). The
endpoint is NOT gated by `HTTP_API_KEY` because scrapers typically
cannot inject arbitrary headers, and the exposition contains no
privileged data.
"""

from __future__ import annotations

from fastapi import APIRouter, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from zashiki_warasi.observability import REGISTRY

router = APIRouter(tags=["observability"])


@router.get("/metrics", include_in_schema=False)
def metrics() -> Response:
    """Render the current scrape body from our REGISTRY.

    `include_in_schema=False` keeps the OpenAPI doc clean — /metrics
    is for scrapers, not human API consumers.
    """
    return Response(
        content=generate_latest(REGISTRY),
        media_type=CONTENT_TYPE_LATEST,
    )
