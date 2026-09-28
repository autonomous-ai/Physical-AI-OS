"""Liveness endpoint shared by dlserver and lbserver.

Must stay trivial (no auth, model checks, downstream calls, locks or I/O): it only
proves the event loop is running. Not readiness; see /hal/api/dl/health for that.
"""

from fastapi import APIRouter

router = APIRouter()


@router.get("/livez")
async def livez() -> dict[str, str]:
    """Return 200 iff a coroutine can still be scheduled on the event loop."""
    return {"status": "alive"}
