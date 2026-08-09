"""POST /reauth — initiate the OAuth web flow.

Session 1 stub: returns 501 Not Implemented. Group 10 (task 10.1-10.3)
lands the real body with CSRF generation + Flow construction + FlowStore
put + auth_url response.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from zashiki_warasi.web.dependencies import require_api_key

router = APIRouter(tags=["reauth"])


@router.post("/reauth", dependencies=[Depends(require_api_key)])
async def reauth() -> dict:
    raise HTTPException(
        status_code=501,
        detail="not_implemented — POST /reauth wiring lands in Group 10",
    )
