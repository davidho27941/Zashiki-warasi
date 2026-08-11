"""`get_services` FastAPI dependency provider."""

from __future__ import annotations

from fastapi import Request

from zashiki_warasi.core.services import Services


def get_services(request: Request) -> Services:
    """Return the process-wide Services container stored on app.state.

    The lifespan populates `request.app.state.services` at startup;
    calling this before lifespan has run (e.g. from a stray test)
    raises AttributeError with a clear-enough traceback.
    """
    return request.app.state.services
