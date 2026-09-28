"""Emotion analysis WebSocket + HTTP endpoints."""

import logging

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import TypeAdapter, ValidationError

from dlserver.models.facial_emotion import (
    EmotionConfigRequest,
    EmotionFrameRequest,
    EmotionHeartBeatRequest,
    EmotionItem,
    EmotionRecognizeRequest,
    EmotionRecognizeResponse,
    EmotionRequest,
    EmotionResponse,
)
from dlserver.utils.common import decode_image, verify_ws_api_key
from dlserver.utils.state import get_emotion_model

logger: logging.Logger = logging.getLogger(__name__)

ws_router: APIRouter = APIRouter()
http_router: APIRouter = APIRouter()
_request_adapter: TypeAdapter[EmotionRequest] = TypeAdapter(EmotionRequest)


@ws_router.websocket("/emotion-analysis/ws")
async def emotion_analysis_ws(websocket: WebSocket):
    """WebSocket endpoint for streaming emotion recognition.

    Accepts JSON messages with a "type" field:
    - {"type": "frame", "task": "emotion", "frame_b64": "<base64>"} — feed a frame
    - {"type": "config", "task": "emotion", "threshold": 0.5} — update threshold
    - {"type": "heartbeat", "task": "emotion"} — keep-alive

    API key is validated from the X-API-Key header on connect.
    """
    if not await verify_ws_api_key(websocket):
        return

    await websocket.accept()

    emotion_model = get_emotion_model()
    if emotion_model is None or not emotion_model.is_ready():
        await websocket.close(code=1011, reason="Facial emotion model not loaded")
        return

    session = None
    try:
        session = await emotion_model.create_session()
        await session.start()
        while True:
            raw: str = await websocket.receive_text()
            try:
                req = _request_adapter.validate_json(raw)
            except ValidationError as e:
                await websocket.send_json({"error": e.errors()})
                continue

            try:
                match req:
                    case EmotionFrameRequest():
                        frame = decode_image(req.frame_b64)
                        result = await session.update(frame)
                        if result is not None:
                            response = EmotionResponse.from_emotion_detection(result)
                            await websocket.send_json(response.model_dump())

                    case EmotionConfigRequest():
                        session.update_config(
                            confidence_threshold=req.threshold,
                            frame_interval=req.frame_interval,
                        )
                        await websocket.send_json({"status": "config_updated"})

                    case EmotionHeartBeatRequest():
                        await websocket.send_json({"status": "ok"})

                    case _:
                        logger.warning("Unknown emotion WS message type: %s", raw[:200])
            except WebSocketDisconnect:
                raise
            except Exception as e:
                logger.exception("Error processing facial emotion WS message")
                await websocket.send_json({"error": "Processing failed"})

    except WebSocketDisconnect:
        logger.info("Facial emotion analysis WebSocket disconnected")
    except Exception:
        logger.exception("Facial emotion WebSocket handler crashed")
    finally:
        if session is not None:
            await session.stop()


@http_router.get("/emotion-labels")
async def list_fer_labels():
    """Return the ordered label list for the active facial emotion model."""
    emotion_model = get_emotion_model()
    if emotion_model is None or not emotion_model.is_ready():
        raise HTTPException(status_code=503, detail="Facial emotion model not loaded")
    return {"labels": emotion_model.labels}


@http_router.post("/emotion-recognize", response_model=EmotionRecognizeResponse)
async def emotion_recognize(req: EmotionRecognizeRequest):
    """Single-shot emotion recognition from a pre-cropped face image."""
    emotion_model = get_emotion_model()
    if emotion_model is None or not emotion_model.is_ready():
        raise HTTPException(status_code=503, detail="Facial emotion model not loaded")

    try:
        face_crop = decode_image(req.image_b64)

        if req.raw:
            # The client gates. Hand back what the model said, even below
            # threshold — dropping it here would hide it from the client's gate.
            emotion = await emotion_model.predict_face(face_crop, gate=False)
            if emotion is None:
                return EmotionRecognizeResponse(detections=[])
            probabilities = emotion.probabilities
        else:
            emotion = await emotion_model.predict_face(face_crop)
            if emotion is None or emotion.confidence < req.threshold:
                return EmotionRecognizeResponse(detections=[])
            probabilities = None

        if req.raw:
            # Raw mode fires once per face per lamp tick (no server-side gate
            # to filter it down); keep this at debug to avoid flooding the
            # shared server's logs.
            logger.debug("[Facial emotion] Detected %s (%.2f)", emotion.emotion, emotion.confidence)
        else:
            logger.info("[Facial emotion] Detected %s (%.2f)", emotion.emotion, emotion.confidence)
        return EmotionRecognizeResponse(
            detections=[
                EmotionItem(
                    emotion=emotion.emotion,
                    confidence=emotion.confidence,
                    face_confidence=emotion.face_confidence,
                    bbox=emotion.bbox,
                    valence=emotion.valence,
                    arousal=emotion.arousal,
                    probabilities=probabilities,
                )
            ]
        )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception("Error processing facial emotion HTTP message")
        raise HTTPException(status_code=500, detail="Internal server error")
