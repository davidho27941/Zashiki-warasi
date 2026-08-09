"""POST /poll — single-flight tick trigger.

Session 1 stub: returns 501 Not Implemented. Group 6 (task 6.1-6.4)
lands the real body with advisory-lock single-flight + `tick_once`
delegation + TickResult JSON body.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["poll"])


@router.post("/poll")
async def poll() -> dict:
    raise HTTPException(
        status_code=501,
        detail="not_implemented — POST /poll wiring lands in Group 6",
    )
