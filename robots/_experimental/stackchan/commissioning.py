"""Opt-in Stack-chan bench API; never mounted by the standard HAL entrypoint."""
from __future__ import annotations

import os
from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, StrictFloat

import hal.app_state as state
from hal.drivers.motors.stackchan_service import (
    StackChanCommandRejected,
    StackChanCommissioningFailed,
    StackChanMotionService,
)

router = APIRouter(prefix="/stackchan", tags=["Experimental Stack-chan commissioning"])


class HomeMoveRequest(BaseModel):
    coordinate_frame: Literal["calibrated_home_deg_v1"]
    positions: dict[str, StrictFloat] = Field(..., description="Pitch-only saved-home target: tilt 7..10 degrees")
    duration: StrictFloat = Field(2.0, ge=2.0, le=10.0)

    model_config = {"extra": "forbid"}


def _service(*, connected: bool = True) -> StackChanMotionService:
    svc = state.animation_service
    if not isinstance(svc, StackChanMotionService):
        raise HTTPException(503, "Stack-chan commissioning requires the Stack-chan motion driver")
    if connected and not svc.is_connected:
        raise HTTPException(503, "Stack-chan is not connected")
    return svc


def _call(method: str, *args):
    svc = _service()
    try:
        return getattr(svc, method)(*args)
    except NotImplementedError as exc:
        raise HTTPException(501, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except StackChanCommandRejected as exc:
        raise HTTPException(502, {"op": exc.op, "code": exc.code, "message": str(exc)}) from exc
    except StackChanCommissioningFailed as exc:
        raise HTTPException(502, exc.details) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/home")
def home_state():
    """Discover capability without a connection, bus read, or motion lease."""
    return _service(connected=False).home_state()


@router.get("/home/position")
def home_position():
    return _call("get_home_positions")


@router.post("/home/move")
def home_move(req: HomeMoveRequest):
    if state._sleeping:
        raise HTTPException(409, "Home commissioning is blocked while sleeping")
    return _call("move_home", req.positions, req.duration)


@router.post("/stop")
def stop():
    # Reuse the common stop's tracker/policy cleanup, translating only this
    # driver's exception at the bench boundary. Standard routes stay unchanged.
    from hal.routes.servo import stop_servos

    _service()
    try:
        return stop_servos()
    except StackChanCommandRejected as exc:
        raise HTTPException(502, {"op": exc.op, "code": exc.code, "message": str(exc)}) from exc


@router.post("/release")
def release():
    svc = _service()
    # Stop background tracking before moving to rest, just as the common
    # release route does. A failed tracker stop must not launch bench motion.
    if state.tracker_service and state.tracker_service.is_tracking:
        try:
            state.tracker_service.stop()
        except Exception as exc:
            raise HTTPException(502, "Unable to stop tracking before release") from exc
    try:
        svc.release_checked()
    except StackChanCommandRejected as exc:
        raise HTTPException(502, {"op": exc.op, "code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        raise HTTPException(502, {"message": "Stack-chan release failed",
                                  "errors": {"stackchan": str(exc)}}) from exc
    return {"status": "ok"}


def create_app():
    """Explicit Uvicorn factory sharing one HAL lifecycle and body connection."""
    if os.getenv("DEVICE_TYPE") != "stackchan" or os.getenv("HAL_SIMULATE") != "0":
        raise RuntimeError("Commissioning requires DEVICE_TYPE=stackchan and HAL_SIMULATE=0")
    from hal.server import app

    # The standard hal.server:app entrypoint never imports this module.
    if not any(route.path == "/stackchan/home" for route in app.routes):
        app.include_router(router)
    return app
