"""Shared FastAPI dependency providers.

`get_services()` is the canonical way handlers access long-lived
collaborators (DB pool, Gmail client, agent, notifier, ...). Tests
override it via `app.dependency_overrides[get_services] = ...`.
"""

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
