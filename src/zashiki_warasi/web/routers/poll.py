"""POST /poll — single-flight tick trigger.

Delegates to `tick_once(services.poller)` under a Postgres advisory
lock so at most one tick is in flight system-wide (across any number
of replicas). Concurrent invocations get 409 Conflict — never
coalesced into the first tick's result, never silently double-run.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from zashiki_warasi.coordination.advisory_lock import (
    TICK_LOCK_KEY,
    advisory_lock,
)
from zashiki_warasi.core.schemas import TickResult
from zashiki_warasi.core.services import Services
from zashiki_warasi.gmail.poller import tick_once
from zashiki_warasi.web.dependencies import get_services, require_api_key

router = APIRouter(tags=["poll"])

# Sentinel returned by the sync tick-runner when the advisory lock is
# already held. Avoids raising/catching an exception on the hot path
# just to convey the boolean.
_CONFLICT = object()


@router.post("/poll", dependencies=[Depends(require_api_key)])
async def poll(services: Services = Depends(get_services)):
    """Execute exactly one tick_once() under the tick advisory lock.

    Returns TickResult (200) on success, `{"reason": "tick_in_flight"}`
    (409) if another replica holds the lock, or lets uncaught exceptions
    propagate to FastAPI's default handler (→ HTTP 500).
    """

    def _do_tick() -> TickResult | object:
        # advisory_lock holds a Postgres connection for the whole with-
        # block so the session-scoped lock covers the tick body. Runs
        # in a threadpool because tick_once is sync + holds a DB conn.
        with advisory_lock(services.checkpointer_pool, TICK_LOCK_KEY) as (
            acquired,
            _conn,
        ):
            if not acquired:
                return _CONFLICT
            return tick_once(services.poller)

    result = await asyncio.to_thread(_do_tick)
    if result is _CONFLICT:
        return JSONResponse(
            status_code=409, content={"reason": "tick_in_flight"}
        )
    # FastAPI serializes the pydantic TickResult natively.
    return result
