import base64
import json
import logging
import os
import shutil
import threading
import time
from collections import Counter
from copy import copy
from dataclasses import dataclass
from typing import Any

import cv2
import requests
from typing_extensions import override

import hal.config as config
from hal.dedup_sidecar import DedupStateSidecar
from hal.drivers.sensing.crypto import CryptoSession, resolve_public_key
from hal.drivers.sensing.perceptions.models import (
    Face,
    FaceDetectionData,
)
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenceState, PresenseService

from .base import Perception
from .emotion_gating import gate_reading, load_label_thresholds

logger = logging.getLogger(__name__)

# Boot-scoped dedup sidecar — survives HAL service restarts so the first
# flush after a deploy/OTA doesn't re-fire the last-known emotion as if it
# were news (same pattern as the motion sidecar). tmpfs + boot_id: a full
# device reboot starts fresh on purpose.
_EMOTION_STATE_PATH = "/tmp/hal-emotion-state.json"

# How long to stop calling the recognize API after a 429 (plan quota
# exhausted) — every call inside the window is a guaranteed failure.
_RATE_LIMIT_BACKOFF_S = 300.0

EMOTIONS = [
    "Neutral",
    "Happy",
    "Sad",
    "Surprise",
    "Fear",
    "Disgust",
    "Anger",
    "Contempt",
]

# Phase 2 bucket dedup: collapse fine-grained labels into polarity buckets.
# Dedup key is (user, bucket) so cross-bucket flips like Fear↔Happy still
# fire (different buckets) but within-bucket noise like Fear↔Sad↔Anger
# collapses to a single "negative" event per 5-min window.
# Outbound message text stays raw ("Emotion detected: Sad.") — variant A,
# minimal-risk: no downstream parsing changes.
EMOTION_BUCKETS = {
    "Happy": "positive",
    "Surprise": "positive",
    "Sad": "negative",
    "Fear": "negative",
    "Anger": "negative",
    "Disgust": "negative",
    "Contempt": "negative",
}

# Per-trigger debug capture. Every emotion-recognizer trigger writes its own
# timestamped folder (input crop + annotated frame + result.json) under
# config.EMOTION_LOG_DIR, so misclassifications can be eyeballed against ground
# truth. Knobs live in hal/config.py with the rest of the HAL environment:
# HAL_EMOTION_DEBUG_LOG_ENABLED / HAL_EMOTION_LOG_DIR / HAL_EMOTION_LOG_MAX_TRIGGERS.


class EmotionDebugLogger:
    """Persists every emotion-recognizer trigger to its OWN timestamped folder
    under ``config.EMOTION_LOG_DIR``, so each prediction can be eyeballed against what
    the person was actually feeling.

    The folder name encodes the verdict up front so a wrong label is obvious
    from the directory listing alone:

      - prediction: ``<timestamp>_<Emotion>_<confidence>`` — e.g.
        ``20260713-090001-123456_Sad_0.42``
      - failure:    ``<timestamp>_FAIL-<reason>`` — e.g.
        ``20260713-090002-004521_FAIL-no-detection``

    Each folder contains:

      - ``input.jpg``     the exact face crop sent to the model (the input)
      - ``frame.jpg``     the clean, unannotated original frame (predictions only)
      - ``annotated.jpg`` full frame with bbox + label drawn (predictions only)
      - ``result.json``   label, confidence, threshold, bbox coords, context, reason

    ``result.json`` records both the raw ``bbox`` and the clamped ``crop_box``
    actually used, so the crop can be reproduced from ``frame.jpg`` (or re-cropped
    with different padding) offline.

    All writes are best-effort and lock-guarded — a logging failure never
    breaks detection. When ``max_triggers > 0`` the oldest folders are pruned
    so the log directory stays bounded.
    """

    def __init__(self, root_dir: str, enabled: bool = True, max_triggers: int = 0):
        self._root: str = root_dir
        self._enabled: bool = enabled
        self._max_triggers: int = max_triggers
        self._lock: threading.Lock = threading.Lock()

    @staticmethod
    def _slug(text: Any) -> str:
        """Filesystem-safe token for a folder name."""
        s = "".join(c if c.isalnum() else "-" for c in str(text)).strip("-")
        return s or "unknown"

    @staticmethod
    def _json_default(o: Any) -> Any:
        """Coerce non-JSON types (numpy scalars/arrays) to native values so
        confidences serialize as numbers, not stringified reprs."""
        if hasattr(o, "item"):  # numpy scalar → python int/float
            return o.item()
        if hasattr(o, "tolist"):  # numpy array → list
            return o.tolist()
        return str(o)

    def _annotate(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        emotion: str,
        confidence: float,
    ) -> cv2.typing.MatLike | None:
        try:
            vis = frame.copy()
            x1, y1, x2, y2 = bbox
            _ = cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 2)
            _ = cv2.putText(
                vis,
                f"{emotion} {confidence:.2f}",
                (x1, max(0, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
            )
            return vis
        except Exception:
            return None

    def _write_folder(
        self,
        name: str,
        record: dict[str, Any],
        images: dict[str, cv2.typing.MatLike | None],
    ) -> str | None:
        if not self._enabled:
            return None
        try:
            with self._lock:
                now = time.time()
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + (
                    "-%06d" % int((now % 1) * 1_000_000)
                )
                folder = os.path.join(self._root, f"{stamp}_{name}")
                os.makedirs(folder, exist_ok=True)
                for filename, image in images.items():
                    if image is not None:
                        _ = cv2.imwrite(os.path.join(folder, filename), image)
                full: dict[str, Any] = {"timestamp": stamp, "ts": now}
                full.update(record)
                # Serialize FIRST — a serialization error (e.g. a circular ref)
                # then raises before the file is opened, so result.json is never
                # left half-written/truncated.
                text = json.dumps(full, indent=2, default=self._json_default)
                with open(
                    os.path.join(folder, "result.json"), "w", encoding="utf-8"
                ) as f:
                    _ = f.write(text)
                self._prune()
            return folder
        except Exception as e:  # never let debug logging break detection
            logger.debug("[activity.emotion] debug save failed: %s", e)
            return None

    def _prune(self) -> None:
        """Drop oldest trigger folders past the cap. Caller holds the lock."""
        if self._max_triggers <= 0:
            return
        try:
            entries = [
                e
                for e in os.listdir(self._root)
                if os.path.isdir(os.path.join(self._root, e))
            ]
            # Names are timestamp-prefixed → lexical sort == chronological.
            entries.sort()
            for stale in entries[: max(0, len(entries) - self._max_triggers)]:
                shutil.rmtree(os.path.join(self._root, stale), ignore_errors=True)
        except Exception as e:
            logger.debug("[activity.emotion] debug prune failed: %s", e)

    def save_prediction(
        self,
        emotion: str,
        confidence: float,
        face_crop: cv2.typing.MatLike,
        frame: cv2.typing.MatLike | None = None,
        bbox: list[int] | None = None,
        **meta: Any,
    ) -> str | None:
        """One trigger the model returned a label for → its own folder."""
        # Bail before _annotate: it copies the whole frame and draws on it, so
        # leaving the check to _write_folder cost a ~2.7MB memcpy per face per
        # tick even with capture switched off.
        if not self._enabled:
            return None
        annotated = (
            self._annotate(frame, bbox, emotion, confidence)
            if frame is not None and bbox is not None
            else None
        )
        name = f"{self._slug(emotion)}_{confidence:.2f}"
        record: dict[str, Any] = {
            "status": "prediction",
            "emotion": emotion,
            "confidence": confidence,
        }
        if bbox is not None:
            record["bbox"] = list(bbox)
        record.update(meta)
        # frame.jpg is the clean original → re-crop from it using bbox/crop_box.
        return self._write_folder(
            name,
            record,
            {"input.jpg": face_crop, "frame.jpg": frame, "annotated.jpg": annotated},
        )

    def save_failure(
        self,
        reason: str,
        face_crop: cv2.typing.MatLike | None = None,
        **meta: Any,
    ) -> str | None:
        """One trigger that produced no usable label → its own folder, with the
        input image (when available) and the reason it failed."""
        if not self._enabled:
            return None
        name = "FAIL-" + self._slug(reason)
        record: dict[str, Any] = {"status": "failure", "reason": reason}
        record.update(meta)
        return self._write_folder(name, record, {"input.jpg": face_crop})


# Sentinel recorded in the attempt history when a recognition attempt produced
# no usable reading (empty response, HTTP error, exception). These frames are
# invisible to _emotion_buffer — they never become an EmotionData — but they
# still happened, and a negative emotion should have to hold its ground against
# them rather than only against the frames that happened to return a label.
_NO_READING = "__none__"


# Trailing span the occupancy test looks back over, and the share of that span a
# label must hold. Matches the flush interval: a real expression lasting less
# than one window is exactly the noise this is meant to drop.
#
# The test is a STRICT majority (`>`), not `>=`. 2/3 was the first setting and
# it was too tight — a genuine Surprise held 3 of 5 attempts and was dropped,
# because ceil(2/3 * 5) = 4. Using `>=` at 0.5 instead would admit ties (2 of
# 4), which costs a false negative-bucket event on the recorded session; a
# strict majority admits the 3-of-5 and still drops every 1-of-5 and 1-of-10,
# which is the noise this exists for.
_OCCUPANCY_LOOKBACK_S: float = 10.0
_OCCUPANCY_MIN_RATIO: float = 0.5

# A majority alone is not enough when the span is tiny. Attempts are counted per
# FACE DETECTION, not per sensing tick — _process_face only runs when a face was
# found — so a face seen once in ten seconds gives a span of 1, and a single Sad
# is then trivially a "majority" (observed on device, 12:42). Two readings is the
# floor: it costs nothing on the recorded session, keeps a brief 2-of-3, and ends
# the 1-of-1. Happy is exempt along with the rest of _INSTANT_LABELS.
_OCCUPANCY_MIN_READINGS: int = 2

# Labels that still fire on a single frame. A smile is frequently one frame at
# this cadence (median ~2.2s between triggers), so requiring persistence would
# drop most genuine ones. The noisy labels are all on the other side of this
# line and must clear _OCCUPANCY_MIN_RATIO instead.
_INSTANT_LABELS: frozenset[str] = frozenset({"Happy"})


class RemoteEmotionRecognizer:
    """Calls the perception-service HTTP emotion-recognize endpoint for a single face crop."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        threshold: float = config.EMOTION_CONFIDENCE_THRESHOLD,
        timeout: float = 10.0,
        debug_logger: "EmotionDebugLogger | None" = None,
        label_thresholds: dict[str, float] | None = None,
    ):
        self._url: str = (
            base_url.rstrip("/") + "/" + config.DL_EMOTION_RECOGNIZE_ENDPOINT.strip("/")
            if base_url
            else ""
        )
        self._api_key: str = api_key
        self._threshold: float = threshold
        # Per-label bars applied here when the server returns raw probabilities.
        self._label_thresholds: dict[str, float] = (
            label_thresholds
            if label_thresholds is not None
            else load_label_thresholds(config.EMOTION_LABEL_THRESHOLDS_JSON)
        )
        self._timeout: float = timeout
        self._crypto: CryptoSession | None = None
        self._debug: EmotionDebugLogger | None = debug_logger
        # Rate-limit backoff — same shape as fire_hazard: a 429 means the
        # plan quota is exhausted, every further call (one per face per
        # sensing tick) is a guaranteed failure + a WARNING log. Pause.
        self._backoff_until: float = 0.0

        if config.DL_ENCRYPTION_ENABLED:
            self._setup_crypto()

    def _setup_crypto(self) -> None:
        """Initialize crypto session for HTTP encryption."""
        public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
        if public_key is None:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")
            logger.warning("[emotion] encryption enabled but no public key — plaintext fallback")
            return
        self._crypto = CryptoSession(public_key)
        logger.info("[emotion] encryption session initialized")

    def _img2b64(self, frame: cv2.typing.MatLike) -> str:
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def recognize(self, face_crop: cv2.typing.MatLike) -> dict[str, Any] | None:
        """Send a face crop to the emotion-recognize endpoint.

        Returns dict with keys: emotion, confidence, valence, arousal,
        probabilities (when the server sent per-class probabilities, i.e. a
        raw-mode response), all_detections (the full candidate list from the
        server), and gate ("hal" when this method applied the device-side
        gate, "server" when an older server already gated before responding).
        Returns None if unavailable, no detection was returned, or the
        reading failed the device gate.
        """
        if not self._url:
            return None
        if time.time() < self._backoff_until:
            return None

        try:
            plain_body = json.dumps({
                "image_b64": self._img2b64(face_crop),
                "threshold": self._threshold,
                # A supporting server skips its own gate and returns every class
                # probability; an older one ignores this and gates as before.
                "raw": True,
            }).encode()

            if self._crypto is not None:
                resp = requests.post(
                    self._url,
                    data=self._crypto.wrap_http_request(plain_body),
                    headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
                    timeout=self._timeout,
                )
            else:
                resp = requests.post(
                    self._url,
                    data=plain_body,
                    headers={"X-API-Key": self._api_key, "Content-Type": "application/json"},
                    timeout=self._timeout,
                )

            if resp.status_code == 429:
                self._backoff_until = time.time() + _RATE_LIMIT_BACKOFF_S
                logger.warning(
                    "[activity.emotion] HTTP 429 (quota) — pausing recognition for %.0fs",
                    _RATE_LIMIT_BACKOFF_S,
                )
                if self._debug is not None:
                    _ = self._debug.save_failure(
                        "http-429",
                        face_crop=face_crop,
                        status=resp.status_code,
                        body=resp.text[:500],
                        threshold=self._threshold,
                        detail=f"quota exhausted — recognition paused {_RATE_LIMIT_BACKOFF_S:.0f}s",
                    )
                return None
            if resp.status_code != 200:
                logger.warning(
                    "[activity.emotion] HTTP %d: %s", resp.status_code, resp.text
                )
                if self._debug is not None:
                    _ = self._debug.save_failure(
                        f"http-{resp.status_code}",
                        face_crop=face_crop,
                        status=resp.status_code,
                        body=resp.text[:500],
                        threshold=self._threshold,
                    )
                return None

            if self._crypto is not None:
                resp_body = self._crypto.unwrap_http_response(resp.content)
                detections = json.loads(resp_body).get("detections", [])
            else:
                detections = resp.json().get("detections", [])
            if not detections:
                # Empty here means the model returned nothing for this crop.
                # A non-raw server would additionally drop a below-threshold
                # detection here, but with raw: true the server skips its own
                # gate and hands back everything the model produced — an empty
                # list only happens when there was no detection to return.
                if self._debug is not None:
                    _ = self._debug.save_failure(
                        "no-detection",
                        face_crop=face_crop,
                        threshold=self._threshold,
                        detail="no detection above confidence threshold",
                    )
                return None

            # Return the top detection (highest confidence) as a shallow COPY
            # with the candidate list attached. Copying is essential: `top` is
            # itself an element of `detections`, so mutating it in place
            # (top["all_detections"] = detections) would create a circular
            # reference that makes json.dump abort mid-write and leave
            # result.json truncated.
            top = max(detections, key=lambda d: d["confidence"])
            result = dict(top)
            result["all_detections"] = detections

            probabilities = top.get("probabilities")
            if not probabilities:
                # Older server: it already gated with its own map.
                result["gate"] = "server"
                return result

            gated = gate_reading(probabilities, self._label_thresholds, self._threshold)
            if gated is None:
                # Same outcome as the old server's empty response: the caller
                # records no reading, which counts against the label in the vote.
                if self._debug is not None:
                    _ = self._debug.save_failure(
                        "gated",
                        face_crop=face_crop,
                        threshold=self._threshold,
                        label_thresholds=self._label_thresholds,
                        probabilities=probabilities,
                        gate="hal",
                        detail=f"{top['emotion']} {top['confidence']:.2f} failed the device gate",
                    )
                return None

            result["emotion"] = gated.label
            result["confidence"] = gated.confidence
            result["gate"] = "hal"
            return result
        except requests.RequestException as e:
            logger.warning("[activity.emotion] request failed: %s", e)
            if self._debug is not None:
                _ = self._debug.save_failure(
                    "request-error", face_crop=face_crop, error=str(e)
                )
            return None


@dataclass
class EmotionData:
    frame: cv2.typing.MatLike
    face: Face
    emotion: str
    confidence: float
    ts: float


class EmotionPerception(Perception[FaceDetectionData]):
    """Detects facial emotions via face recognizer callback + perception-service HTTP.

    Registers a callback with FaceRecognizer. When a face is detected,
    sends the face crop to the emotion-recognize HTTP endpoint. Buffers
    results per-person and flushes aggregated emotion events periodically.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None,
        base_url: str = config.DL_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)

        self._presence_service: PresenseService | None = presense_service
        self._base_url: str = base_url
        self._api_key: str = api_key

        self._debug: EmotionDebugLogger = EmotionDebugLogger(
            root_dir=config.EMOTION_LOG_DIR,
            enabled=config.EMOTION_DEBUG_LOG_ENABLED,
            max_triggers=config.EMOTION_LOG_MAX_TRIGGERS,
        )
        if config.EMOTION_DEBUG_LOG_ENABLED:
            logger.info("[activity.emotion] debug logging → %s", config.EMOTION_LOG_DIR)

        self._recognizer: RemoteEmotionRecognizer = RemoteEmotionRecognizer(
            base_url=base_url,
            api_key=api_key,
            threshold=config.EMOTION_CONFIDENCE_THRESHOLD,
            debug_logger=self._debug,
        )

        self._last_detection_time: float | None = None
        self._last_emotion: str | None = None

        # Lock protects all mutable state below
        self._state_lock: threading.RLock = threading.RLock()

        # Retained: EMOTION_FLUSH_S no longer gates the decision (see
        # _flush_buffer — readings are judged as they arrive), but the value is
        # still the documented cadence knob and to_dict/consumers read it.
        self._flush_interval: float = config.EMOTION_FLUSH_S
        self._last_flush_ts: float = 0.0
        # {person_id: [emotion_str, ...]}
        self._emotion_buffer: dict[str, list[EmotionData]] = {}

        # Dedup: TTL map per (current_user, emotion) — repeated key inside
        # window dropped even if other emotions were sent in between.
        # Last-key-only dedup let alternating sad/fear/sad/fear bypass the
        # window and spam the agent queue every flush.
        # Restored from the boot-scoped sidecar so a service restart doesn't
        # wipe the TTL map and re-fire the last emotion on the first flush.
        # The debug-only _last_sent_key stays None after a restore — the
        # reset_dedup user-change guard then no-ops, which is harmless: keys
        # are (user, bucket), so a new user always forms fresh keys anyway.
        self._sidecar: DedupStateSidecar = DedupStateSidecar(
            _EMOTION_STATE_PATH, "activity.emotion"
        )
        self._last_sent_by_key: dict[tuple[str, str], float] = self._sidecar.load()
        self._last_sent_key: tuple[str, str] | None = None  # debug/to_dict
        self._last_sent_ts: float = 0.0  # debug/to_dict
        self._dedup_window_s: float = config.EMOTION_DEDUP_WINDOW_S

        # Every recognition attempt, including the ones that produced nothing:
        # (ts, person_id, label-or-_NO_READING). Pruned to the lookback on
        # append rather than at flush — _flush_buffer returns early when the
        # buffer is empty WITHOUT advancing _last_flush_ts, so pruning there
        # would stall during a quiet spell and leave a stale denominator.
        self._attempt_history: list[tuple[float, str, str]] = []

    def _record_attempt(self, person_id: str, label: str) -> None:
        """Log one recognition attempt and drop anything past the lookback."""
        now = time.time()
        with self._state_lock:
            self._attempt_history.append((now, person_id, label))
            cutoff = now - _OCCUPANCY_LOOKBACK_S
            self._attempt_history = [
                a for a in self._attempt_history if a[0] >= cutoff
            ]

    def _process_face(
        self,
        frame: cv2.typing.MatLike,
        face: Face,
    ) -> None:
        """Crop face, send to emotion backend, buffer result."""

        h, w = frame.shape[:2]
        # Prefer the face-mesh re-centered box computed during face recognition
        # (get_box over the dense 468 landmarks): tighter and better-centered on
        # the face than the raw detector bbox, and the framing the cloud emotion
        # model expects, with NO rotation applied. Fall back to the detector bbox
        # when no mesh box is available (e.g. the v1 recognizer). Both are
        # [x1, y1, x2, y2].
        box_source = "emotion_box" if face.emotion_box is not None else "bbox"
        x1, y1, x2, y2 = face.emotion_box if face.emotion_box is not None else face.bbox

        # Clamp to frame bounds
        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)
        if x2 <= x1 or y2 <= y1:
            return

        face_crop = frame[y1:y2, x1:x2]

        try:
            result = self._recognizer.recognize(face_crop)
        except Exception as e:
            logger.exception("[activity.emotion] recognize error")
            _ = self._debug.save_failure(
                "recognize-exception",
                face_crop=face_crop,
                person_id=face.person_id,
                error=str(e),
            )
            self._record_attempt(face.person_id, _NO_READING)
            return

        if result is None:
            # A failure folder (http error / no-detection / request error) was
            # already written inside recognize() with the precise reason.
            # An empty response is NOT evidence of Neutral — the gate that
            # rejected the argmax was either ours (emotion_gating, against a
            # raw-mode server) or the server's own (an older server that
            # ignores raw and gates before responding), so this is "no
            # confirmed reading", and it counts against any label trying to
            # claim the window.
            self._record_attempt(face.person_id, _NO_READING)
            return

        emotion = result["emotion"]
        confidence = result["confidence"]


        self._record_attempt(face.person_id, emotion)

        if self._presence_service:
            self._presence_service.on_motion()

        with self._state_lock:
            self._last_detection_time = time.time()
            self._last_emotion = emotion

            if face.person_id not in self._emotion_buffer:
                self._emotion_buffer[face.person_id] = []

            weighted_confidence = confidence * face.confidence
            self._emotion_buffer[face.person_id].append(
                EmotionData(
                    frame=frame,
                    face=face,
                    emotion=emotion,
                    confidence=weighted_confidence,
                    ts=self._last_detection_time,
                )
            )

        # Every trigger the model labelled → its own timestamped folder holding
        # the exact input crop, an annotated frame, and result.json. Folder name
        # is "<time>_<Emotion>_<conf>" so a wrong prediction is spottable at a
        # glance against what the person was actually feeling.
        _ = self._debug.save_prediction(
            emotion=emotion,
            confidence=confidence,
            face_crop=face_crop,
            frame=frame,
            bbox=face.bbox,
            # Clamped [x1, y1, x2, y2] actually used for the crop — apply this to
            # frame.jpg to reproduce input.jpg (bbox above is the raw, unclamped
            # detector box, which may extend past the frame edges).
            crop_box=[x1, y1, x2, y2],
            # Which box produced the crop: the face-mesh re-centered box (reused
            # from recognition) or the raw detector bbox fallback.
            box_source=box_source,
            frame_size=[w, h],
            person_id=face.person_id,
            face_confidence=face.confidence,
            weighted_confidence=weighted_confidence,
            threshold=config.EMOTION_CONFIDENCE_THRESHOLD,
            valence=result.get("valence"),
            arousal=result.get("arousal"),
            all_detections=result.get("all_detections"),
            gate=result.get("gate"),
        )

        logger.debug(
            "[activity.emotion] %s: %s (%.2f)", face.person_id, emotion, confidence
        )

    @override
    def cleanup(self) -> None:
        pass

    @override
    def _check_impl(self, data: FaceDetectionData) -> None:
        """Only used for periodic flush — actual detection is callback-driven."""
        if data.frame is not None:
            logger.debug("[emotion] processing %d face(s)", len(data.faces))
            for f in data.faces:
                self._process_face(data.frame, f)
        else:
            logger.debug("[emotion] frame is None, skipping detection")

        self._flush_buffer()

    def _save_annotated(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        emotion: str,
        confidence: float,
    ) -> cv2.typing.MatLike | None:
        """Draw annotation and save to snapshot dir. Rotates old files."""
        try:
            os.makedirs(config.EMOTION_SNAPSHOT_DIR, exist_ok=True)

            vis = frame.copy()
            x1, y1, x2, y2 = bbox
            _ = cv2.rectangle(vis, (x1, y1), (x2, y2), (0, 255, 0), 2)
            label = f"{emotion} {confidence:.2f}"
            _ = cv2.putText(
                vis,
                label,
                (x1, y1 - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 255, 0),
                2,
            )

            return vis
        except Exception as e:
            logger.debug("[activity.emotion] snapshot save failed: %s", e)
            return None

    def _flush_buffer(self) -> None:
        """Decide on every tick, over the window the occupancy test measures.

        This used to run at most once per EMOTION_FLUSH_S and clear the buffer
        wholesale. Both parts were wrong. The callback that drives it only fires
        when a face is DETECTED (faceid/perception.py returns early otherwise),
        so when someone looks away mid-expression the remaining readings sat
        unevaluated until the next face appeared — 91s in the case observed on
        2026-09-04 12:55. By then their attempts had aged out of the trailing
        window and the vote read "Surprise held 0/1". The failure selects for
        SHORT expressions: a sustained Anger keeps the callback firing and lands
        a flush while it is still fresh, a two-second Surprise does not.

        So: evaluate whenever a reading arrives, and hold readings only for as
        long as the occupancy window that judges them. Rate limiting is already
        the job of the (user, bucket) dedup, not of this interval.
        """
        with self._state_lock:
            if not self._emotion_buffer:
                return

            cur_ts = time.time()

            # Drop readings older than the window the vote is measured over —
            # they can never be supported by it, and keeping them is what
            # produced the "0/N" drops.
            for pid in list(self._emotion_buffer):
                fresh = [
                    ed
                    for ed in self._emotion_buffer[pid]
                    if (cur_ts - ed.ts) <= _OCCUPANCY_LOOKBACK_S
                ]
                if fresh:
                    self._emotion_buffer[pid] = fresh
                else:
                    del self._emotion_buffer[pid]
            if not self._emotion_buffer:
                return

            buffer = copy(self._emotion_buffer)
            self._last_flush_ts = cur_ts

        if (
            self._presence_service is not None
            and self._presence_service.state != PresenceState.PRESENT
        ):
            logger.info(
                "[activity.emotion] skipping — no presence (presence=%s)",
                self._presence_service.state,
            )
            return

        # Dedup key uses the global current_user (same source of truth as
        # MotionPerception + reset_dedup). Per-face person_id is too noisy
        # ('?' / 'stranger_17' / 'stranger_18' all flip the key on every
        # frame and bypass dedup). Skip entirely when current_user is "":
        # nobody is in scene (no friend, no stranger within forget window)
        # so there's no subject to attribute emotion to.
        current_user = self._perception_state.current_user.data or ""
        if not current_user:
            logger.info("[activity.emotion] skipping — no current_user (scene empty)")
            return

        # Prune expired entries from the TTL map once per flush.
        cutoff = cur_ts - self._dedup_window_s
        with self._state_lock:
            self._last_sent_by_key = {
                k: ts for k, ts in self._last_sent_by_key.items() if ts >= cutoff
            }

        # Process each person's emotions
        for person_id, emotion_data_list in buffer.items():
            if emotion_data_list:
                logger.info(
                    "[activity.emotion] %s raw: %s",
                    person_id,
                    ", ".join([d.emotion for d in emotion_data_list]),
                )

            # Skip Neutral
            non_neutral = [
                (ed.emotion, ed.confidence)
                for ed in emotion_data_list
                if ed.emotion != "Neutral"
            ]
            if not non_neutral:
                continue

            counts = Counter(e for e, _ in non_neutral)

            # A candidate has to earn the window. _INSTANT_LABELS fire on a
            # single frame (unchanged behaviour); everything else must hold
            # _OCCUPANCY_MIN_RATIO of this person's attempts over the trailing
            # _OCCUPANCY_LOOKBACK_S — attempts that returned nothing included.
            # Without that denominator a lone Anger frame among a dozen failures
            # wins the vote outright, which is how a man working at his desk
            # produced sustained "Anger" events.
            with self._state_lock:
                span = [
                    label
                    for ts, pid, label in self._attempt_history
                    if pid == person_id and (cur_ts - ts) <= _OCCUPANCY_LOOKBACK_S
                ]
            need = _OCCUPANCY_MIN_RATIO * len(span)

            qualified: list[tuple[int, str]] = []
            for label, hits in counts.items():
                if label in _INSTANT_LABELS:
                    qualified.append((hits, label))
                    continue
                occupancy = sum(1 for x in span if x == label)
                if occupancy > need and occupancy >= _OCCUPANCY_MIN_READINGS:
                    qualified.append((occupancy, label))
                else:
                    why = (
                        "not a majority"
                        if occupancy <= need
                        else "only %d reading(s), needs %d"
                        % (occupancy, _OCCUPANCY_MIN_READINGS)
                    )
                    logger.info(
                        "[activity.emotion] %s dropped: %s held %d/%d attempts "
                        "in the last %.0fs — %s",
                        person_id,
                        label,
                        occupancy,
                        len(span),
                        _OCCUPANCY_LOOKBACK_S,
                        why,
                    )
            if not qualified:
                continue

            dominant_emotion = max(qualified, key=lambda q: q[0])[1]

            # Average confidence over instances of the dominant label only —
            # other labels' confidences would dilute it.
            dom_confidences = [c for e, c in non_neutral if e == dominant_emotion]
            avg_confidence = sum(dom_confidences) / len(dom_confidences)

            # Phase 2: dedup by polarity bucket, not raw label. Fear↔Sad
            # ↔Anger noise within the same bucket collapses to one event
            # per window; cross-bucket flips (Fear→Happy) still fire as a
            # genuine mood change. "other" bucket catches any label not in
            # EMOTION_BUCKETS so unknown emotions still self-dedup.
            bucket = EMOTION_BUCKETS.get(dominant_emotion, "other")

            # Hedge prevents LLM over-commit on noisy FER reads. Raw
            # "Emotion detected: <Name>." prefix kept for skill parser.
            hedge = {
                "negative": "do not assume the user is distressed",
                "positive": "do not over-celebrate",
            }.get(bucket, "do not over-react")
            message = (
                f"Emotion detected: {dominant_emotion}. "
                f"(weak camera cue; confidence={avg_confidence:.2f}; "
                f"bucket={bucket}; treat as uncertain, {hedge}.)"
            )

            key = (current_user, bucket)
            with self._state_lock:
                last_ts = self._last_sent_by_key.get(key)
                if last_ts is not None and (cur_ts - last_ts) < self._dedup_window_s:
                    logger.info(
                        "[activity.emotion] dedup drop: %s bucket=%s (key seen %.1fs ago)",
                        message,
                        bucket,
                        cur_ts - last_ts,
                    )
                    continue
                self._last_sent_by_key[key] = cur_ts
                self._last_sent_key = key
                self._last_sent_ts = cur_ts
                self._sidecar.save(self._last_sent_by_key)

            # Annotating copies a frame per matching reading, so it happens
            # only for an event that is actually going out. It used to run
            # before the dedup check, which was cheap while the vote ran once
            # per EMOTION_FLUSH_S and wasteful once it began running per tick —
            # most of those evaluations are dedup drops.
            snapshots = [
                self._save_annotated(ed.frame, ed.face.bbox, ed.emotion, ed.confidence)
                for ed in emotion_data_list
                if ed.emotion == dominant_emotion
            ]
            snapshots = [s for s in snapshots if s is not None]

            logger.info("[activity.emotion] flushing: %s", message)
            self._send_event("emotion.detected", message, "emotion", snapshots, None)

            # Only the person that fired is cleared. Everyone else keeps their
            # readings so a label still building toward a majority is not reset
            # by someone else's event.
            with self._state_lock:
                _ = self._emotion_buffer.pop(person_id, None)

    def reset_dedup(self, new_user: str = "") -> None:
        """Clear the outbound dedup state only if the visible user actually
        changed. Mirrors MotionPerception.reset_dedup — called by
        SensingService on presence.enter via the orchestrator. Without this
        guard, every stranger flicker would wipe the key and bypass the
        5-min window.
        """
        with self._state_lock:
            if self._last_sent_key is None:
                return
            last_user = self._last_sent_key[0]
            if last_user == new_user:
                logger.debug(
                    "[activity.emotion] dedup reset skipped — same user %r",
                    last_user,
                )
                return
            logger.info(
                "[activity.emotion] dedup reset (user %r → %r, %d keys cleared)",
                last_user,
                new_user,
                len(self._last_sent_by_key),
            )
            self._last_sent_by_key.clear()
            self._last_sent_key = None
            self._last_sent_ts = 0.0
            # The outgoing user's attempts must not count toward the new one's
            # occupancy test.
            self._attempt_history.clear()
            # Sync the sidecar (unlinks it) so a restart can't resurrect the
            # state this user-change reset just cleared.
            self._sidecar.save(self._last_sent_by_key)

    def to_dict(self) -> dict[str, Any]:
        with self._state_lock:
            seconds_since = (
                int(time.time() - self._last_detection_time)
                if self._last_detection_time is not None
                else None
            )
            last_sent = self._last_sent_key
            return {
                "type": "emotion",
                "last_sent_emotion": last_sent[1] if last_sent else None,
                "last_sent_user": last_sent[0] if last_sent else None,
                "last_detected_emotion": self._last_emotion,
                "buffered_persons": len(self._emotion_buffer),
                "dedup_keys": len(self._last_sent_by_key),
                "emotion_detected": self._last_detection_time is not None,
                "seconds_since_detection": seconds_since,
            }
