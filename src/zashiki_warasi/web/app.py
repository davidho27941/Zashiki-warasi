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

from zashiki_warasi.core.config import (
    ObservabilitySettings,
    warn_removed_env_vars,
)
from zashiki_warasi.core.logging import configure_logging
from zashiki_warasi.core.services import build_services, close_services
from zashiki_warasi.observability.tracing import configure_tracing
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
    # OTel bootstrap runs BEFORE build_services so the psycopg
    # instrumentation is in place before the checkpointer pool opens
    # its first connection (else those queries wouldn't be spanned).
    # Also runs the WEB_CONCURRENCY guard — if multi-worker was
    # requested, we exit here before touching Postgres.
    configure_tracing(ObservabilitySettings())
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
    # OTel FastAPI auto-instrumentation. Attaches middleware that reads
    # the CURRENT tracer_provider at request time — so calling this
    # before configure_tracing() (which runs in lifespan) is safe: the
    # NoOp provider used at import time is replaced with the real one
    # before the first request. When OTEL_ENABLED=0 the current
    # provider stays NoOp and this middleware is a cheap wrapper doing
    # no export work.
    _instrument_fastapi(application)
    return application


def _instrument_fastapi(application: FastAPI) -> None:
    """Best-effort FastAPI instrumentation. Lazy-imports so a test env
    that stripped OTel deps doesn't break app creation."""
    try:
        from opentelemetry.instrumentation.fastapi import (
            FastAPIInstrumentor,
        )
    except ImportError:
        return
    try:
        FastAPIInstrumentor.instrument_app(application)
    except Exception:
        logger.exception(
            "FastAPI OTel instrumentation failed; continuing without "
            "server spans"
        )


app = create_app()
