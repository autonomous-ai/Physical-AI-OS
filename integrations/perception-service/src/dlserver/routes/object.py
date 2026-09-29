"""Object detection endpoints per enabled detector: POST /object-detect/{name}, WS /object-detection/{name}/ws."""

import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from dlserver.models.object import (
    ObjectConfigRequest,
    ObjectDetectionItemResponse,
    ObjectDetectRequest,
    ObjectDetectResponse,
    ObjectFrameRequest,
    ObjectHeartBeatRequest,
    ObjectRequest,
    ObjectResponse,
)
from dlserver.utils.common import decode_image, verify_ws_api_key
from dlserver.utils.state import get_object_model, get_object_models

logger: logging.Logger = logging.getLogger(__name__)

ws_router: APIRouter = APIRouter()
http_router: APIRouter = APIRouter()
_request_adapter: TypeAdapter[ObjectRequest] = TypeAdapter(ObjectRequest)


@ws_router.websocket("/object-detection/{detector_name}/ws")
async def object_detection_ws(websocket: WebSocket, detector_name: str):
    """WebSocket endpoint for streaming object detection.

    Message types: ``frame``, ``config`` (classes), ``heartbeat``.
    """
    if not await verify_ws_api_key(websocket):
        return

    await websocket.accept()

    object_model = get_object_model(detector_name)
    if object_model is None or not object_model.is_ready():
        await websocket.close(code=1011, reason=f"Object detector '{detector_name}' not loaded")
        return

    session = None
    try:
        session = await object_model.create_session()
        while True:
            raw: str = await websocket.receive_text()
            try:
                req = _request_adapter.validate_json(raw)
            except ValidationError as e:
                await websocket.send_json({"error": e.errors()})
                continue

            try:
                match req:
                    case ObjectFrameRequest():
                        frame = decode_image(req.frame_b64)
                        result = await session.update(frame)
                        if result is not None:
                            response = ObjectResponse.from_object_detection(result)
                            await websocket.send_json(response.model_dump())

                    case ObjectConfigRequest():
                        session.update_config(
                            frame_interval=req.frame_interval,
                            classes=req.classes,
                            threshold=req.threshold,
                        )
                        await websocket.send_json({"status": "config_updated"})

                    case ObjectHeartBeatRequest():
                        await websocket.send_json({"status": "ok"})

                    case _:
                        logger.warning("Unknown object WS message type: %s", raw[:200])
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.exception("Error processing object detection WS message")
                await websocket.send_json({"error": "Processing failed"})

    except WebSocketDisconnect:
        logger.info("Object detection WebSocket disconnected (%s)", detector_name)
    except Exception:
        logger.exception("Object detection WebSocket handler crashed (%s)", detector_name)
    finally:
        if session is not None:
            await session.stop()


@http_router.post("/object-detect/{detector_name}", response_model=ObjectDetectResponse)
async def object_detect(detector_name: str, req: ObjectDetectRequest):
    """Single-shot object detection from a base64-encoded image."""
    object_model = get_object_model(detector_name)
    if object_model is None or not object_model.is_ready():
        raise HTTPException(status_code=503, detail=f"Object detector '{detector_name}' not loaded")

    try:
        frame = decode_image(req.image_b64)
        result = await object_model.predict_image(frame, classes=req.classes)

        logger.info("[Object/%s] Detected %d objects", detector_name, len(result.detections))
        return ObjectDetectResponse.from_object_detection(result)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Error processing object detection HTTP message")
        raise HTTPException(status_code=500, detail="Internal server error")


@http_router.post("/{detector_name}", response_model=list[ObjectDetectionItemResponse])
async def object_detect_compat(detector_name: str, req: ObjectDetectRequest):
    """Backward-compatible flat-list endpoint (matches go2 /api/dl/yoloworld format)."""
    object_model = get_object_model(detector_name)
    if object_model is None or not object_model.is_ready():
        raise HTTPException(status_code=503, detail=f"Object detector '{detector_name}' not loaded")

    try:
        frame = decode_image(req.image_b64)
        result = await object_model.predict_image(frame, classes=req.classes)

        logger.info("[Object/%s] Detected %d objects", detector_name, len(result.detections))
        return [
            ObjectDetectionItemResponse(
                class_name=d.class_name,
                xywh=d.xywh,
                confidence=d.confidence,
            )
            for d in result.detections
        ]
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Error processing object detection HTTP message")
        raise HTTPException(status_code=500, detail="Internal server error")


@http_router.get("/object-detect/models")
async def list_object_models():
    """List all loaded object detectors."""
    models = get_object_models()
    return {"models": [{"name": name, "ready": model.is_ready()} for name, model in models.items()]}
