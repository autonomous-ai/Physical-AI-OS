"""Shared utilities for protocol handlers."""

import base64
import secrets

import cv2
import cv2.typing as cv2t
import numpy as np
from fastapi import WebSocket

from config import settings


def decode_image(image_b64: str) -> cv2t.MatLike:
    """Decode a base64 JPEG/PNG to a BGR array; raises ValueError on invalid input."""
    img_bytes = base64.b64decode(image_b64)
    max_bytes = settings.input_limits.max_image_bytes
    if len(img_bytes) > max_bytes:
        raise ValueError(f"Image exceeds {max_bytes // (1024 * 1024)} MB limit")
    img_array = np.frombuffer(img_bytes, dtype=np.uint8)
    image = cv2.imdecode(img_array, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Failed to decode image")
    h, w = image.shape[:2]
    max_dim = settings.input_limits.max_image_dim
    if h > max_dim or w > max_dim:
        raise ValueError(f"Image dimensions {w}x{h} exceed {max_dim}x{max_dim} limit")
    return image


async def verify_ws_api_key(websocket: WebSocket) -> bool:
    """Validate the API key on WebSocket connect; closes the socket and returns False if invalid."""
    if settings.dl_api_key:
        api_key = websocket.headers.get("x-api-key", "")
        if not api_key or not secrets.compare_digest(api_key, settings.dl_api_key):
            await websocket.close(code=1008, reason="Invalid or missing API key")
            return False
    return True
