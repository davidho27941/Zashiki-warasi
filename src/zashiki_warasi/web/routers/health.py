"""GET /healthz — real dependency-truth endpoint.

Session 1 stub: returns 200 with `{"status": "healthy"}`. The full
DB + OAuth truth checks land in Group 5 (task 5.1) which replaces
this handler body without changing the route.
"""

from __future__ import annotations

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    return {"status": "healthy", "checks": {}}
