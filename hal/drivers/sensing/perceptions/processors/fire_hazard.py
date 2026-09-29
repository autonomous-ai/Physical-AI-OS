"""Fire hazard detection via DL backend object detection endpoint."""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, override

import cv2
import cv2.typing as cv2t
import numpy as np
import numpy.typing as npt
import requests

import hal.config as config
from hal.drivers.sensing.crypto import CryptoSession, resolve_public_key
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenseService

from .base import Perception
from .models import FireHazard, FireHazardEnum
from .utils import bbox_area, bbox_intersection, img2b64, xywh_to_xyxy

logger = logging.getLogger(__name__)

# How long to stop calling the detection API after a 429 (plan quota
# exhausted). Every call inside the window is a guaranteed failure anyway;
# safety is unchanged — the backend can't detect anything while limited.
_RATE_LIMIT_BACKOFF_S = 300.0

RESOURCES_DIR = Path(__file__).parent / "resources"

class RemoteFireHazardDetector:
    """HTTP client for the DL backend object detection endpoint."""

    DEFAULT_FIRE_CLASSES_PATH: Path = RESOURCES_DIR / "fire_classes.txt"
    DEFAULT_SMOKE_CLASSES_PATH: Path = RESOURCES_DIR / "smoke_classes.txt"
    DEFAULT_FLAMMABLE_CLASSES_PATH: Path = RESOURCES_DIR / "flammable_classes.txt"
    DEFAULT_SAFE_CLASSES_PATH: Path = RESOURCES_DIR / "safe_classes.txt"

    def __init__(
        self,
        base_url: str = config.FIRE_HAZARD_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
        timeout: float = config.FIRE_HAZARD_API_TIMEOUT_S,
    ):
        self._url: str = base_url
        self._api_key: str = api_key
        self._timeout: float = timeout
        self._crypto: CryptoSession | None = None
        self._backoff_until: float = 0.0

        self._fire_classes: list[str] = self._load_classes(self.DEFAULT_FIRE_CLASSES_PATH)
        self._smoke_classes: list[str] = self._load_classes(self.DEFAULT_SMOKE_CLASSES_PATH)
        self._flammable_classes: list[str] = self._load_classes(self.DEFAULT_FLAMMABLE_CLASSES_PATH)
        self._safe_classes: list[str] = self._load_classes(self.DEFAULT_SAFE_CLASSES_PATH)
        self._all_classes: list[str] = (
            self._fire_classes + self._smoke_classes + self._flammable_classes + self._safe_classes
        )

        self._fire_set: frozenset[str] = frozenset(self._fire_classes)
        self._smoke_set: frozenset[str] = frozenset(self._smoke_classes)
        self._flammable_set: frozenset[str] = frozenset(self._flammable_classes)
        self._safe_set: frozenset[str] = frozenset(self._safe_classes)

        if config.DL_ENCRYPTION_ENABLED:
            self._setup_crypto()

        logger.info(
            "[fire_hazard] detector initialized — %d fire, %d smoke, %d flammable, %d safe classes, endpoint=%s",
            len(self._fire_classes), len(self._smoke_classes),
            len(self._flammable_classes), len(self._safe_classes), self._url,
        )

    @staticmethod
    def _load_classes(path: Path) -> list[str]:
        if not path.exists():
            logger.warning("[fire_hazard] class file not found: %s", path)
            return []
        return [line.strip() for line in path.read_text().strip().splitlines() if line.strip()]

    def _setup_crypto(self) -> None:
        public_key = resolve_public_key(
            config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE
        )
        if public_key is None:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")
            logger.warning("[fire_hazard] encryption enabled but no public key — plaintext fallback")
            return
        self._crypto = CryptoSession(public_key)
        logger.info("[fire_hazard] encryption session initialized")

    def detect(self, frame: cv2t.MatLike) -> list[FireHazard]:
        """Detect objects in a frame and assess fire hazard risk."""
        if not self._url:
            return []

        objects = self._call_api(frame)

        objects = [
            o for o in objects
            if o.get("confidence", 0) >= config.FIRE_HAZARD_CONFIDENCE_THRESHOLD
        ]

        if not objects:
            return []

        fire_objects = [o for o in objects if o.get("class_name", "") in self._fire_set]
        smoke_objects = [o for o in objects if o.get("class_name", "") in self._smoke_set]
        flammable_objects = [o for o in objects if o.get("class_name", "") in self._flammable_set]
        safe_objects = [o for o in objects if o.get("class_name", "") in self._safe_set]

        logger.debug(
            "[fire_hazard] detections: %d fire, %d smoke, %d flammable, %d safe",
            len(fire_objects), len(smoke_objects), len(flammable_objects), len(safe_objects),
        )

        return self._assess_hazard(fire_objects, smoke_objects, flammable_objects, safe_objects)

    def _call_api(self, frame: cv2t.MatLike) -> list[dict[str, Any]]:
        """POST frame to the object detection endpoint."""
        if time.time() < self._backoff_until:
            return []
        try:
            plain_body = json.dumps({
                "image_b64": img2b64(frame),
                "classes": self._all_classes,
            }).encode()

            headers: dict[str, str] = {"Content-Type": "application/json"}
            if self._api_key:
                headers["X-API-Key"] = self._api_key

            if self._crypto is not None:
                resp = requests.post(
                    self._url,
                    data=self._crypto.wrap_http_request(plain_body),
                    headers=headers,
                    timeout=self._timeout,
                )
            else:
                resp = requests.post(
                    self._url,
                    data=plain_body,
                    headers=headers,
                    timeout=self._timeout,
                )

            if resp.status_code == 429:
                self._backoff_until = time.time() + _RATE_LIMIT_BACKOFF_S
                logger.warning(
                    "[fire_hazard] HTTP 429 (quota) — pausing detection for %.0fs: %s",
                    _RATE_LIMIT_BACKOFF_S, resp.text[:120],
                )
                return []
            if resp.status_code != 200:
                logger.warning("[fire_hazard] HTTP %d: %s", resp.status_code, resp.text[:200])
                return []

            if self._crypto is not None:
                data = json.loads(self._crypto.unwrap_http_response(resp.content))
            else:
                data = resp.json()

            if isinstance(data, dict):
                return data.get("detections", [])
            return data

        except requests.RequestException as e:
            logger.warning("[fire_hazard] request failed: %s", e)
            return []
        except Exception as e:
            logger.exception("[fire_hazard] unexpected error: %s", e)
            return []

    def _assess_hazard(
        self,
        fire_objects: list[dict[str, Any]],
        smoke_objects: list[dict[str, Any]],
        flammable_objects: list[dict[str, Any]],
        safe_objects: list[dict[str, Any]],
    ) -> list[FireHazard]:
        """Assess fire hazard risk from detected objects."""
        hazards: list[FireHazard] = []

        for o in smoke_objects:
            hazards.append(FireHazard(
                type=FireHazardEnum.SMOKE,
                bbox=xywh_to_xyxy(np.array(o["xywh"], dtype=np.float32)),
            ))

        fire_bboxes = np.array(
            [xywh_to_xyxy(np.array(o["xywh"], dtype=np.float32)) for o in fire_objects]
        ) if fire_objects else np.zeros((0, 4), dtype=np.float32)

        flammable_bboxes = np.array(
            [xywh_to_xyxy(np.array(o["xywh"], dtype=np.float32)) for o in flammable_objects]
        ) if flammable_objects else np.zeros((0, 4), dtype=np.float32)

        safe_bboxes = np.array(
            [xywh_to_xyxy(np.array(o["xywh"], dtype=np.float32)) for o in safe_objects]
        ) if safe_objects else np.zeros((0, 4), dtype=np.float32)

        if len(fire_bboxes) > 0:
            fire_bboxes = fire_bboxes[bbox_area(fire_bboxes) > 1e-5]
        if len(flammable_bboxes) > 0:
            flammable_bboxes = flammable_bboxes[bbox_area(flammable_bboxes) > 1e-5]
        if len(safe_bboxes) > 0:
            safe_bboxes = safe_bboxes[bbox_area(safe_bboxes) > 1e-5]

        n_fire = fire_bboxes.shape[0]
        n_flammable = flammable_bboxes.shape[0]
        n_safe = safe_bboxes.shape[0]

        if n_fire == 0:
            return hazards

        if n_flammable > 0:
            flammable_overlap = bbox_intersection(
                fire_bboxes[:, None, :], flammable_bboxes[None, :, :]
            ) / np.minimum(
                bbox_area(fire_bboxes[:, None, :]), bbox_area(flammable_bboxes[None, :, :])
            )
            flammable_score = flammable_overlap.max(axis=-1)
        else:
            flammable_score = np.full(n_fire, -1.0)

        if n_safe > 0:
            safe_overlap = bbox_intersection(
                fire_bboxes[:, None, :], safe_bboxes[None, :, :]
            ) / np.minimum(
                bbox_area(fire_bboxes[:, None, :]), bbox_area(safe_bboxes[None, :, :])
            )
            safe_score = safe_overlap.max(axis=-1)
        else:
            safe_score = np.full(n_fire, -1.0)

        threshold = config.FIRE_HAZARD_OVERLAP_THRESHOLD
        is_safe = np.logical_and(safe_score > flammable_score, safe_score > threshold)
        is_hazard = np.logical_and(flammable_score >= safe_score, flammable_score > threshold)

        for safe, hazard, bbox in zip(is_safe, is_hazard, fire_bboxes):
            if safe:
                hazards.append(FireHazard(type=FireHazardEnum.SAFE_FIRE, bbox=bbox))
            elif hazard:
                hazards.append(FireHazard(type=FireHazardEnum.HAZARD_FIRE, bbox=bbox))
            else:
                hazards.append(FireHazard(type=FireHazardEnum.UNSURE_FIRE, bbox=bbox))

        return hazards


class FireHazardPerception(Perception[cv2t.MatLike]):
    """Detects fire hazards by calling the object detection API on each frame."""

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        base_url: str = config.FIRE_HAZARD_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)
        self._presense_service = presense_service
        self._detector = RemoteFireHazardDetector(base_url=base_url, api_key=api_key)
        self._last_check_ts: float = 0.0

        self._state_lock: threading.RLock = threading.RLock()
        self._last_sent_by_type: dict[str, float] = {}
        self._dedup_window_s: float = config.FIRE_HAZARD_DEDUP_WINDOW_S

        self._first_seen_by_type: dict[str, float] = {}
        self._confirm_s: float = config.FIRE_HAZARD_CONFIRM_S

        self._hazard_buffer: list[FireHazard] = []
        self._snapshot_buffer: list[npt.NDArray[np.uint8]] = []
        self._flush_interval: float = config.FIRE_HAZARD_FLUSH_S
        self._last_flush_ts: float = 0.0

    @override
    def _check_impl(self, data: cv2t.MatLike) -> None:
        if data is None:
            logger.debug("[fire_hazard] frame is None, skipping")
            return

        now = time.time()

        if now - self._last_check_ts < config.FIRE_HAZARD_CHECK_INTERVAL_S:
            self._flush_buffer(now)
            return
        self._last_check_ts = now

        hazards = self._detector.detect(data)

        with self._state_lock:
            seen_types: set[str] = set()
            confirmed: list[FireHazard] = []

            for h in hazards:
                t = h.type.value
                seen_types.add(t)

                if t not in self._first_seen_by_type:
                    self._first_seen_by_type[t] = now
                    logger.debug("[fire_hazard] first seen: %s", t)

                elapsed = now - self._first_seen_by_type[t]
                if elapsed >= self._confirm_s:
                    confirmed.append(h)

            for t in list(self._first_seen_by_type):
                if t not in seen_types:
                    logger.debug("[fire_hazard] streak broken: %s", t)
                    del self._first_seen_by_type[t]

            if confirmed:
                annotated = self._annotate_frame(data, confirmed)
                self._hazard_buffer.extend(confirmed)
                self._snapshot_buffer.append(annotated)

        self._flush_buffer(now)

    def _flush_buffer(self, now: float) -> None:
        with self._state_lock:
            if not self._hazard_buffer:
                return
            if (now - self._last_flush_ts) < self._flush_interval:
                return

            hazards = list(self._hazard_buffer)
            snapshots = list(self._snapshot_buffer)
            self._hazard_buffer.clear()
            self._snapshot_buffer.clear()
            self._last_flush_ts = now

        cutoff = now - self._dedup_window_s
        with self._state_lock:
            self._last_sent_by_type = {
                k: ts for k, ts in self._last_sent_by_type.items() if ts >= cutoff
            }

        by_type: dict[str, list[FireHazard]] = {}
        for hazard in hazards:
            by_type.setdefault(hazard.type.value, []).append(hazard)

        new_types: list[str] = []
        for hazard_type in by_type:
            with self._state_lock:
                last_ts = self._last_sent_by_type.get(hazard_type)
                if last_ts is not None and (now - last_ts) < self._dedup_window_s:
                    logger.debug(
                        "[fire_hazard] dedup drop: %s (seen %.1fs ago)",
                        hazard_type, now - last_ts,
                    )
                    continue
                self._last_sent_by_type[hazard_type] = now
            new_types.append(hazard_type)

        if not new_types:
            return

        types_summary = ", ".join(sorted(new_types))
        message = f"Fire hazard detected: {types_summary}"
        logger.info("[fire_hazard] %s (%d snapshots)", message, len(snapshots))
        self._send_event("fire_hazard.detected", message, "fire_hazard", snapshots, None)

    _HAZARD_COLORS: dict[FireHazardEnum, tuple[int, int, int]] = {
        FireHazardEnum.HAZARD_FIRE: (0, 0, 255),
        FireHazardEnum.UNSURE_FIRE: (0, 165, 255),
        FireHazardEnum.SAFE_FIRE: (0, 200, 0),
        FireHazardEnum.SMOKE: (128, 128, 128),
    }

    def _annotate_frame(
        self, frame: cv2t.MatLike, hazards: list[FireHazard]
    ) -> npt.NDArray[np.uint8]:
        """Draw hazard bboxes (xyxy) and labels on a copy of the frame."""
        vis: npt.NDArray[np.uint8] = frame.copy()
        for h in hazards:
            x1, y1, x2, y2 = h.bbox.astype(int)
            color = self._HAZARD_COLORS.get(h.type, (0, 255, 255))
            cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            cv2.putText(vis, h.type.value, (x1, y1 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.6, color, 2)
        return vis

    def reset_dedup(self, new_user: str = "") -> None:
        pass

    @override
    def to_dict(self) -> dict[str, Any]:
        with self._state_lock:
            return {
                "type": "fire_hazard",
                "buffered_hazards": len(self._hazard_buffer),
                "buffered_snapshots": len(self._snapshot_buffer),
                "confirmed_types": list(self._first_seen_by_type.keys()),
                "dedup_keys": len(self._last_sent_by_type),
            }

    @override
    def cleanup(self) -> None:
        pass
