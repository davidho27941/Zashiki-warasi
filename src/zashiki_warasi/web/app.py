"""FastAPI application factory.

Import target: `uvicorn zashiki_warasi.web:app`. The lifespan hook
runs `configure_logging()` + `build_services()` on startup and
`close_services()` on shutdown, stashing the container on
`app.state.services` so handlers can pull it via `get_services`.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from zashiki_warasi.core.config import warn_removed_env_vars
from zashiki_warasi.core.logging import configure_logging
from zashiki_warasi.core.services import build_services, close_services
from zashiki_warasi.web.middleware.request_id import RequestIdMiddleware
from zashiki_warasi.web.routers import auth, health, metrics, poll, reauth

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Bootstrap on startup, tear down on shutdown.

    `build_services()` is synchronous but idempotently safe to call
    from an async context — it just opens a Postgres pool and wires
    up in-memory objects. No async work is needed.
    """
    configure_logging()
    warn_removed_env_vars(logger)
    services = build_services()
    app.state.services = services
    logger.info("FastAPI service ready")
    try:
        yield
    finally:
        logger.info("FastAPI service shutting down")
        close_services(services)


def create_app() -> FastAPI:
    """Factory used by tests that want a fresh instance with mocked
    dependencies. Production loads the module-level `app` below."""
    application = FastAPI(
        title="Zashiki-warasi",
        description=(
            "Self-hosted Gmail polling AI email agent. External "
            "scheduler drives cadence via POST /poll; /healthz reflects "
            "real dependency readiness."
        ),
        lifespan=lifespan,
    )
    # Request-id middleware runs first so its ContextVar is set before
    # any handler / dependency / log line executes for the request.
    application.add_middleware(RequestIdMiddleware)
    application.include_router(health.router)
    application.include_router(metrics.router)
    application.include_router(poll.router)
    application.include_router(auth.router)
    application.include_router(reauth.router)
    return application


app = create_app()
