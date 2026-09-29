"""Action analysis WebSocket endpoint."""

import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from dlserver.models.action import (
    ActionConfigRequest,
    ActionFrameRequest,
    ActionHeartBeatRequest,
    ActionRequest,
    ActionResponse,
)
from dlserver.utils.common import decode_image, verify_ws_api_key
from dlserver.utils.state import get_action_model

logger = logging.getLogger(__name__)

router = APIRouter()
_request_adapter = TypeAdapter(ActionRequest)


@router.websocket("/action-analysis/ws")
async def action_analysis_ws(websocket: WebSocket):
    """WebSocket endpoint for streaming action recognition.

    Message types: ``frame``, ``config`` (``whitelist``; null resets). Requires X-API-Key.
    """
    if not await verify_ws_api_key(websocket):
        return

    await websocket.accept()

    action_model = get_action_model()
    if action_model is None or not action_model.is_ready():
        await websocket.close(code=1011, reason="Action model not loaded")
        return

    session = None
    try:
        session = await action_model.create_session()
        while True:
            raw = await websocket.receive_text()
            try:
                req = _request_adapter.validate_json(raw)
            except ValidationError as e:
                await websocket.send_json({"error": e.errors()})
                continue

            try:
                match req:
                    case ActionFrameRequest():
                        frame = decode_image(req.frame_b64)
                        result = await session.update(frame)
                        if result is not None:
                            response = ActionResponse.from_human_action_detection(result)
                            await websocket.send_json(response.model_dump())

                    case ActionConfigRequest():
                        session.update_config(
                            whitelist=req.whitelist,
                            threshold=req.threshold,
                            person_detection_enabled=req.person_detection_enabled,
                            person_min_area_ratio=req.person_min_area_ratio,
                        )
                        await websocket.send_json({"status": "config_updated"})
                    case ActionHeartBeatRequest():
                        await websocket.send_json({"status": "ok"})

                    case _:
                        logger.warning("Unknown action WS message type: %s", raw[:200])
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.exception("Error processing action WS message")
                await websocket.send_json({"error": "Processing failed"})

    except WebSocketDisconnect:
        logger.info("Action analysis WebSocket disconnected")
    except Exception:
        logger.exception("Action analysis WebSocket handler crashed")
    finally:
        if session is not None:
            await session.stop()
