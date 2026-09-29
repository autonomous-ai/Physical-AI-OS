"""Learned-policy route handlers (dry-run only: never imports LeRobot or moves a servo)."""
from fastapi import APIRouter, HTTPException

import hal.app_state as state
from hal.models import (
    PolicyRunRequest,
    PolicyRunResponse,
    PolicyStatusResponse,
    PolicyStopResponse,
)

router = APIRouter(tags=["Policy"])


def _svc():
    if state.policy_service is None:
        raise HTTPException(503, "Policy service not available")
    return state.policy_service


def _response(run):
    return {
        "status": "accepted",
        "id": run.id,
        "policy": run.policy,
        "task": run.task,
        "state": run.state,
        "dry_run": run.dry_run,
    }


@router.post("/policy/run", response_model=PolicyRunResponse)
def run_policy(req: PolicyRunRequest):
    """Record a learned-policy request without starting inference or motion."""
    try:
        return _response(_svc().run(req.policy, req.task))
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


@router.get("/policy", response_model=PolicyStatusResponse)
def get_policy_status():
    """Return the active dry-run request, if one has been accepted."""
    run = _svc().active_run()
    return {"active": _response(run) if run is not None else None}


@router.post("/policy/stop", response_model=PolicyStopResponse)
def stop_policy():
    """Cancel a dry-run policy request without touching a motor."""
    run = _svc().stop()
    if run is None:
        return {"status": "idle", "dry_run": True}
    return {"status": "stopped", "id": run.id, "dry_run": run.dry_run}
