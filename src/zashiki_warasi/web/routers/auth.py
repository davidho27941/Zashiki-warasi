"""GET /auth/start and GET /auth/callback — OAuth web flow endpoints.

Session 1 stubs: return 501 Not Implemented. Group 9 (task 9.1-9.5)
lands the real bodies with FlowStore lookup + Google redirect +
token persistence.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(prefix="/auth", tags=["auth"])


@router.get("/start", name="auth_start")
async def auth_start(csrf: str) -> None:
    raise HTTPException(
        status_code=501,
        detail="not_implemented — GET /auth/start wiring lands in Group 9",
    )


@router.get("/callback", name="auth_callback")
async def auth_callback(code: str, state: str) -> None:
    raise HTTPException(
        status_code=501,
        detail="not_implemented — GET /auth/callback wiring lands in Group 9",
    )
