import base64
import json
import logging
import os
import threading
import time
from copy import copy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, override

import cv2
import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

import hal.config as config
from hal.drivers.sensing.crypto import CryptoSession, WSKeyExchangeRequest, resolve_public_key
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import (
    PerceptionStateObservers,
    record_dl_stall,
)
from hal.drivers.sensing.presence_service import PresenceState, PresenseService

from .base import Perception
from .pose import PosePerception

logger = logging.getLogger(__name__)

RESOURCES_DIR = Path(__file__).parent / "resources"

_MOTION_STATE_PATH = Path("/tmp/hal-motion-state.json")


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return ""


ACTIVITY_GROUP: dict[str, str] = {
    "drinking": "drink",
    "drinking beer": "drink",
    "drinking shots": "drink",
    "tasting beer": "drink",
    "stretching arm": "break",
    "stretching leg": "break",
    "celebrating": "celebrate",
    "clapping": "celebrate",
    "applauding": "celebrate",
    "tasting food": "eat",
    "dining": "eat",
    "eating burger": "eat",
    "eating cake": "eat",
    "eating carrots": "eat",
    "eating doughnuts": "eat",
    "eating hotdog": "eat",
    "eating ice cream": "eat",
    "eating spaghetti": "eat",
    "eating watermelon": "eat",
    "using computer": "sedentary",
    "writing": "sedentary",
    "texting": "sedentary",
    "reading book": "sedentary",
    "reading newspaper": "sedentary",
    # reading book + reading newspaper both emit the generic label "reading" (see
    # _RAW_LABEL_EMIT_REMAP).
    "reading": "sedentary",
    "drawing": "sedentary",
    "playing controller": "sedentary",
    "yawning": "tired",
    "laughing": "emotional",
    "crying": "emotional",
    "singing": "emotional",
}

_RAW_LABEL_GROUPS: frozenset[str] = frozenset({"sedentary", "eat", "tired"})


def coarse_classes(labels: set[str]) -> frozenset[str]:
    """Coarse activity classes for the cooldown floor's transition bypass."""
    core = frozenset(
        ACTIVITY_GROUP.get(label, label)
        for label in labels
        if ACTIVITY_GROUP.get(label) != "tired"
    )
    return core or frozenset({"tired"})


# Some raw Kinetics labels are folded to a coarser spoken label at emit time while still
# routing through their real bucket.
_RAW_LABEL_EMIT_REMAP: dict[str, str] = {
    "reading book": "reading",
    "reading newspaper": "reading",
}


class MoveEnum(Enum):
    BACKGROUND = (
        "background"
    )
    FOREGROUND = "foreground"
    NONE = "none"


@dataclass
class MotionDetection:
    class_name: str
    conf: float


@dataclass
class ActionResponse:
    detected_classes: list[MotionDetection]


class RemoteMotionChecker:
    """Video action recognition-based motion detector."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        whitelist: list[str] | None = None,
        threshold: float = config.MOTION_CONFIDENCE_THRESHOLD,
        person_detection_enabled: bool = config.MOTION_PERSON_DETECTION_ENABLED,
        person_min_area_ratio: float = config.MOTION_PERSON_MIN_AREA_RATIO,
    ):
        self._base_url: str = base_url
        self._api_key: str = api_key
        self._whitelist: list[str] | None = whitelist
        self._threshold: float = threshold
        self._person_detection_enabled: bool = person_detection_enabled
        self._person_min_area_ratio: float = person_min_area_ratio
        self._ws_session: ClientConnection | None = None
        self._crypto: CryptoSession | None = None

        self._prepare_session()

        self._last_action: str | None = None
        self._last_heartbeat_ts: float = 0.0
        self._heartbeat_interval: float = config.DL_HEARTBEAT_INTERVAL_S

    def _prepare_session(self):
        if self._ws_session is not None:
            logger.info("[%s] has been started", self.__class__.__name__)
            return

        try:
            ws_url = self._base_url.replace("http", "ws").replace("https", "wss")
            logger.info("[%s] connecting to %s", self.__class__.__name__, ws_url)
            self._ws_session = connect(
                ws_url,
                additional_headers={"X-API-Key": self._api_key},
                open_timeout=config.DL_WS_RECV_TIMEOUT_S,
            )
            self._crypto = None
            if config.DL_ENCRYPTION_ENABLED:
                self._setup_crypto()

            config_msg = json.dumps(
                {
                    "type": "config",
                    "task": "action",
                    "whitelist": self._whitelist,
                    "threshold": self._threshold,
                    "person_detection_enabled": self._person_detection_enabled,
                    "person_min_area_ratio": self._person_min_area_ratio,
                }
            )
            if self._crypto is not None:
                config_msg = self._crypto.wrap_ws_message(config_msg)
            self._ws_session.send(config_msg)
            raw = self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S)
            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)
        except Exception:
            logger.exception("Failed to connect to remote motion recognition backend")
            self._ws_session = None

    def _setup_crypto(self) -> None:
        """Perform WS key exchange after connection."""
        if self._ws_session is None:
            raise RuntimeError("Cannot setup crypto without a WS connection")

        public_key = resolve_public_key(config.DL_PUBLIC_KEY_URL, config.DL_API_KEY, config.DL_PUBLIC_KEY_FILE)
        if public_key is None:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError("Encryption required but no public key available")
            logger.warning("[%s] encryption enabled but no public key — plaintext fallback", self.__class__.__name__)
            return

        session = CryptoSession(public_key)
        key_req = WSKeyExchangeRequest(encrypted_key=session.encrypted_key_b64)
        self._ws_session.send(key_req.model_dump_json())
        resp = json.loads(self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S))
        if resp.get("status") == "key_exchange_ok":
            self._crypto = session
            logger.info("[%s] encryption session established", self.__class__.__name__)
        else:
            if config.DL_ENCRYPTION_REQUIRED:
                raise RuntimeError(f"Key exchange failed: {resp}")
            logger.warning("[%s] key exchange failed: %s — plaintext fallback", self.__class__.__name__, resp)

    def _img2b64(self, frame: cv2.typing.MatLike):
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def _send_heartbeat(self) -> None:
        """Send a heartbeat if the interval has elapsed."""
        now = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return

        self._last_heartbeat_ts = now

        if self._ws_session is None:
            return
        try:
            msg = json.dumps({"type": "heartbeat", "task": "action"})
            if self._crypto is not None:
                msg = self._crypto.wrap_ws_message(msg)
            self._ws_session.send(msg)
            raw = self._ws_session.recv()
            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)
            resp = json.loads(raw)
            if resp.get("status") == "ok":
                logger.debug("[motion] heartbeat ok")
            else:
                logger.warning("[motion] heartbeat unexpected response: %s", resp)
        except (ConnectionClosed, TimeoutError):
            logger.warning("[motion] heartbeat failed — connection lost")
            self._ws_session = None

    def update(self, frame: cv2.typing.MatLike) -> list[MotionDetection] | None:
        """Send a frame for action recognition inference."""

        if self._ws_session is None:
            self._prepare_session()
            if self._ws_session is not None:
                logger.info(
                    "[%s] reconnected to %s", self.__class__.__name__, self._base_url
                )

        self._send_heartbeat()

        if self._ws_session is not None:
            try:
                msg = json.dumps(
                    {
                        "type": "frame",
                        "task": "action",
                        "frame_b64": self._img2b64(frame),
                    }
                )
                if self._crypto is not None:
                    msg = self._crypto.wrap_ws_message(msg)

                self._ws_session.send(msg)
                raw = self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S)

                if self._crypto is not None:
                    raw = self._crypto.unwrap_ws_message(raw)

                resp = json.loads(raw)
                detected_classes = sorted(
                    resp.get("detected_classes", []),
                    key=lambda x: x["conf"],
                    reverse=True,
                )
                return [
                    MotionDetection(class_name=dc["class_name"], conf=dc["conf"])
                    for dc in detected_classes
                ]
            except TimeoutError:
                # Backend accepted the frame but never replied within the
                # budget. Drop the session so the worker is freed immediately
                # instead of blocking forever and starving other perceptions.
                logger.warning(
                    "[%s] recv timeout (%.1fs) — dropping session, retry next tick",
                    self.__class__.__name__,
                    config.DL_WS_RECV_TIMEOUT_S,
                )
                record_dl_stall(
                    "motion", f"frame recv timeout {config.DL_WS_RECV_TIMEOUT_S:.0f}s"
                )
                self._ws_session = None
            except ConnectionClosed:
                logger.warning(
                    "[%s] connection lost, will retry on next tick",
                    self.__class__.__name__,
                )
                self._ws_session = None

        return None

    def ready(self):
        return self._ws_session is not None

    def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws_session is not None:
            try:
                self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None

    @property
    def last_action(self) -> str | None:
        return self._last_action


class MotionPerception(Perception[cv2.typing.MatLike]):
    """Detects motion via remote DL backend action recognition."""

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        base_url: str = config.DL_MOTION_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)
        self._presense_service: PresenseService | None = presense_service
        self._last_motion_time: float | None = None

        whitelist = self._load_whitelist()

        self._checker: RemoteMotionChecker = RemoteMotionChecker(
            base_url=base_url,
            api_key=api_key,
            whitelist=whitelist,
            threshold=config.MOTION_CONFIDENCE_THRESHOLD,
        )

        self._flush_interval: float = config.MOTION_FLUSH_S
        self._last_flush_ts: float = 0.0
        self._snapshot_paths: list[str] = []
        self._snapshots_buffer: list[cv2.typing.MatLike] = []
        self._actions_buffer: list[str] = []

        # Dedup state for outbound motion.activity events. User change flips the key
        # immediately; different strangers collapse to "unknown" so they don't break
        # dedup on their own.
        self._last_sent_key: tuple[str, frozenset[str]] | None = None
        self._last_sent_ts: float = 0.0
        self._dedup_window_s: float = 300.0

        self._event_cooldown_s: float = config.MOTION_EVENT_COOLDOWN_S
        self._transition_min_gap_s: float = config.MOTION_TRANSITION_MIN_GAP_S
        self._last_event_ts: float = 0.0
        self._last_sent_class: frozenset[str] | None = None
        self._load_dedup_state()

        self._state_lock: threading.RLock = threading.RLock()

        self._pose_perception: PosePerception | None = None
        self._sedentary_streak_start_ts: float = 0.0

    def _load_dedup_state(self) -> None:
        """Restore dedup/cooldown state from the boot-scoped sidecar."""
        try:
            if not _MOTION_STATE_PATH.exists():
                return
            data = json.loads(_MOTION_STATE_PATH.read_text())
            if data.get("boot_id") != _current_boot_id():
                _MOTION_STATE_PATH.unlink(missing_ok=True)
                return
            self._last_sent_key = (
                str(data.get("user", "")),
                frozenset(data.get("labels") or []),
            )
            self._last_sent_ts = float(data.get("sent_ts") or 0.0)
            self._last_event_ts = float(data.get("event_ts") or 0.0)
            classes = data.get("classes")
            self._last_sent_class = frozenset(classes) if classes else None
            logger.info(
                "[motion] dedup state restored (last event %.0fs ago — no re-fire)",
                time.time() - self._last_event_ts,
            )
        except Exception as e:
            logger.warning("[motion] dedup state load failed: %s", e)

    def _persist_dedup_state(self) -> None:
        """Write the sidecar. Called only on send/reset — a few writes per hour."""
        try:
            if self._last_sent_key is None:
                _MOTION_STATE_PATH.unlink(missing_ok=True)
                return
            user, labels = self._last_sent_key
            _MOTION_STATE_PATH.write_text(
                json.dumps(
                    {
                        "boot_id": _current_boot_id(),
                        "user": user,
                        "labels": sorted(labels),
                        "sent_ts": self._last_sent_ts,
                        "event_ts": self._last_event_ts,
                        "classes": sorted(self._last_sent_class or ()),
                    }
                )
            )
        except Exception as e:
            logger.warning("[motion] dedup state save failed: %s", e)

    def set_pose_perception(self, pose: PosePerception | None) -> None:
        """Wire in the pose sampler so motion can fold posture summaries into outbound
        activity events.
        """
        self._pose_perception = pose

    @staticmethod
    def _load_whitelist() -> list[str] | None:
        whitelist_path = RESOURCES_DIR / "white_list.txt"
        if not whitelist_path.exists():
            logger.warning("[motion] whitelist file not found: %s", whitelist_path)
            return None
        lines = whitelist_path.read_text().strip().splitlines()
        whitelist = [line.strip() for line in lines if line.strip()]
        logger.info("[motion] loaded %d whitelist entries", len(whitelist))
        return whitelist

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        frame = data
        if frame is None:
            logger.debug("[motion] frame is None, skipping")
            return

        # Presence gate for the remote stream: while AWAY (nobody seen for
        # AWAY_TIMEOUT_S) don't send frames to the action-recognition backend.
        if (
            self._presense_service is not None
            and self._presense_service.state == PresenceState.AWAY
        ):
            return

        try:
            detections = self._checker.update(frame)
        except Exception:
            logger.exception("[motion] inference error")
            return

        if detections:
            logger.debug("[motion] detections: %s", [d.class_name for d in detections])
        else:
            logger.debug("[motion] no detections")

        # Annotate + JPEG-encode + disk write BEFORE taking the state lock.
        snapshot_path: str | None = None
        if detections:
            snapshot_path = self._save_annotated(frame, detections)

        with self._state_lock:
            if detections:
                self._last_motion_time = time.time()
                if self._presense_service is not None:
                    self._presense_service.on_motion()

                self._snapshots_buffer.append(frame)
                self._actions_buffer.extend([d.class_name for d in detections])
                if snapshot_path:
                    self._snapshot_paths.append(snapshot_path)

            self._flush_buffer()

    def _draw_annotations(
        self, frame: cv2.typing.MatLike, detections: list[MotionDetection]
    ) -> cv2.typing.MatLike:
        """Draw detected action labels on a copy of the frame."""
        vis = frame.copy()
        y_offset = 30
        for det in detections:
            label = f"{det.class_name} ({det.conf:.2f})"
            _ = cv2.putText(
                vis,
                label,
                (10, y_offset),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 255, 0),
                2,
            )
            y_offset += 30
        return vis

    def _save_annotated(
        self, frame: cv2.typing.MatLike, detections: list[MotionDetection]
    ) -> str | None:
        """Draw annotations and save to snapshot dir. Rotates old files."""
        try:
            os.makedirs(config.MOTION_SNAPSHOT_DIR, exist_ok=True)

            annotated = self._draw_annotations(frame, detections)
            filename = f"motion_{int(time.time() * 1000)}.jpg"
            filepath = os.path.join(config.MOTION_SNAPSHOT_DIR, filename)
            _, buf = cv2.imencode(".jpg", annotated, [cv2.IMWRITE_JPEG_QUALITY, 85])
            with open(filepath, "wb") as f:
                _ = f.write(buf.tobytes())

            files = sorted(
                (
                    os.path.join(config.MOTION_SNAPSHOT_DIR, f)
                    for f in os.listdir(config.MOTION_SNAPSHOT_DIR)
                    if f.endswith(".jpg")
                ),
                key=os.path.getmtime,
            )
            while len(files) > config.MOTION_SNAPSHOT_MAX_COUNT:
                try:
                    os.remove(files.pop(0))
                except OSError:
                    pass

            return filepath
        except Exception as e:
            logger.debug("[motion] snapshot save failed: %s", e)
            return None

    def _flush_buffer(self) -> None:
        with self._state_lock:
            if not self._snapshots_buffer:
                return

            cur_ts = time.time()
            if (cur_ts - self._last_flush_ts) < self._flush_interval:
                return

            actions = copy(self._actions_buffer)
            snapshots_buffer = copy(self._snapshots_buffer)
            self._snapshots_buffer.clear()
            self._snapshot_paths.clear()
            self._actions_buffer.clear()
            self._last_flush_ts = cur_ts

        if actions:
            logger.info("[motion] raw actions in window: %s", actions)

        labels: set[str] = set()

        for a in reversed(actions):
            group = ACTIVITY_GROUP.get(a)
            if group is None:
                logger.warning("[motion] unmapped action '%s', skipping", a)
                continue
            if group == "emotional":
                continue
            if group in _RAW_LABEL_GROUPS:
                labels.add(_RAW_LABEL_EMIT_REMAP.get(a, a))
            else:
                labels.add(group)

        if not labels:
            return

        if (
            self._presense_service is not None
            and self._presense_service.state != PresenceState.PRESENT
        ):
            logger.info(
                "[motion] skipping event — no presence (presence=%s)",
                self._presense_service.state,
            )
            return

        tired_only: bool = bool(labels) and all(
            ACTIVITY_GROUP.get(label) == "tired" for label in labels
        )
        has_sedentary: bool = any(
            ACTIVITY_GROUP.get(label) == "sedentary" for label in labels
        )
        if tired_only:
            pass
        elif has_sedentary:
            if self._sedentary_streak_start_ts <= 0:
                self._sedentary_streak_start_ts = cur_ts
            # Sedentary is the SOLE trigger to open the pose tumbling window. Once the
            # window is open it runs purely on POSE_WINDOW_DURATION_S; later stretch
            # breaks don't stop the clock, they just leave the bad_ratio honest.
            if self._pose_perception is not None:
                self._pose_perception.start_window()
        else:
            if self._sedentary_streak_start_ts > 0:
                logger.debug(
                    "[motion] sedentary streak ended after %.1f min (labels=%s)",
                    (cur_ts - self._sedentary_streak_start_ts) / 60.0,
                    sorted(labels),
                )
            self._sedentary_streak_start_ts = 0.0

        message = f"Activity detected: {', '.join(sorted(labels))}."

        # Posture tumbling-window evaluation. Only two gates remain at fold time:
        # bad_ratio over the configured threshold, and the user must still be sedentary
        # on this flush (don't nag mid-stretch).
        posture_injected: bool = False
        if (
            self._pose_perception is not None
            and self._pose_perception.is_window_complete()
        ):
            if has_sedentary and self._sedentary_streak_start_ts > 0:
                streak_s: float = cur_ts - self._sedentary_streak_start_ts
                streak_min: int = int(streak_s / 60)
                summary: dict[str, Any] | None = (
                    self._pose_perception.get_posture_summary()
                )
                if summary is not None and summary["bad_ratio"] >= config.POSE_BAD_RATIO:
                    summary_with_streak: dict[str, Any] = dict(summary)
                    summary_with_streak["streak_min"] = streak_min
                    # Bucket pointers are surfaced as separate markers (not inside
                    # posture_summary) so the OS server handler can lift them off the
                    # message before stripping for the LLM — the agent never sees the
                    # file paths.
                    bucket_id: str = str(summary.get("bucket_id", "") or "")
                    worst_snaps: list[str] = list(summary.get("worst_snapshots") or [])
                    # Don't ride bucket info inside the LLM-facing summary
                    # JSON either — pop it so the agent sees only the
                    # posture stats it actually reasons about.
                    summary_with_streak.pop("bucket_id", None)
                    summary_with_streak.pop("worst_snapshots", None)
                    message = (
                        f"{message}\n"
                        f"[computer_streak_min: {streak_min}]\n"
                        f"[posture_summary: "
                        f"{json.dumps(summary_with_streak, separators=(',', ':'))}]"
                    )
                    if bucket_id:
                        message = f"{message}\n[pose_bucket: {bucket_id}]"
                    if worst_snaps:
                        message = (
                            f"{message}\n[pose_worst: "
                            f"{','.join(worst_snaps)}]"
                        )
                    posture_injected = True
                    logger.info(
                        "[motion] folding posture summary "
                        "(streak=%dm bad_ratio=%.2f dominant=%s samples=%d bucket=%s worst=%d)",
                        streak_min,
                        summary["bad_ratio"],
                        summary["dominant_region"],
                        summary["samples"],
                        bucket_id,
                        len(worst_snaps),
                    )
            # Unconditional reset — a completed window with no fire (stretch break in
            # progress, bad_ratio under threshold, or too few samples for the noise
            # floor) still must clear, otherwise the next sit-down would evaluate stale
            # data from the previous cycle.
            self._pose_perception.reset_window()

        # Global cooldown floor: within the same coarse activity class, don't emit more
        # than once per _event_cooldown_s.
        classes: frozenset[str] = coarse_classes(labels)
        class_changed: bool = (
            self._last_sent_class is not None and classes != self._last_sent_class
        )
        transition_bypass: bool = class_changed and (
            (cur_ts - self._last_event_ts) >= self._transition_min_gap_s
        )
        if (
            not posture_injected
            and not transition_bypass
            and self._last_sent_key is not None
            and (cur_ts - self._last_event_ts) < self._event_cooldown_s
        ):
            logger.info(
                "[motion] cooldown drop: %s (last event %.1fs ago < %.0fs floor, "
                "class %s)",
                message,
                cur_ts - self._last_event_ts,
                self._event_cooldown_s,
                "changed but < %.0fs transition gap" % self._transition_min_gap_s
                if class_changed
                else "unchanged",
            )
            return
        if transition_bypass:
            logger.info(
                "[motion] transition bypass: %s → %s (last event %.1fs ago)",
                sorted(self._last_sent_class or ()),
                sorted(classes),
                cur_ts - self._last_event_ts,
            )

        current_user = self._perception_state.current_user.data or ""

        key = (current_user, frozenset(labels))
        if (
            self._last_sent_key == key
            and (cur_ts - self._last_sent_ts) < self._dedup_window_s
            and not posture_injected
        ):
            logger.info(
                "[motion] dedup drop: %s (same as last send %.1fs ago)",
                message,
                cur_ts - self._last_sent_ts,
            )
            return
        if posture_injected and self._last_sent_key == key and (cur_ts - self._last_sent_ts) < self._dedup_window_s:
            logger.info(
                "[motion] dedup BYPASS (posture nudge): would-have-dropped (last send %.1fs ago)",
                cur_ts - self._last_sent_ts,
            )
        self._last_sent_key = key
        self._last_sent_ts = cur_ts
        self._last_event_ts = cur_ts
        self._last_sent_class = classes
        self._persist_dedup_state()

        # Log each outbound label to the OS server wellbeing BEFORE firing the event.
        self._post_wellbeing_labels(current_user, labels)

        logger.info("[motion] flushing: %s", message)

        self._send_event("motion.activity", message, "motion_activity",[snapshots_buffer[-1]], None)

    def _post_wellbeing_labels(self, user: str, labels: set[str]) -> None:
        """POST each activity label to the OS server wellbeing log.

        Fires synchronously but with a short timeout so a stuck OS server never blocks
        motion detection.
        """
        log_user = user or "unknown"
        for label in sorted(labels):
            try:
                resp = requests.post(
                    config.OS_WELLBEING_LOG_URL,
                    json={"action": label, "notes": "", "user": log_user},
                    timeout=2,
                )
                if resp.status_code != 200:
                    logger.warning(
                        "[motion] wellbeing log %s returned %d: %s",
                        label,
                        resp.status_code,
                        resp.text,
                    )
            except requests.RequestException as e:
                logger.debug("[motion] wellbeing log %s failed: %s", label, e)

    @override
    def cleanup(self) -> None:
        self._checker.close()

    def reset_dedup(self, new_user: str = "") -> None:
        """Clear the outbound dedup state only if the visible user actually changed."""
        if self._last_sent_key is None:
            return
        last_user = self._last_sent_key[0]
        if last_user == new_user:
            logger.debug(
                "[motion] dedup reset skipped — same user %r",
                last_user,
            )
            return
        logger.info(
            "[motion] dedup reset (user %r → %r)",
            last_user,
            new_user,
        )
        self._last_sent_key = None
        self._last_sent_ts = 0.0
        self._last_event_ts = 0.0
        self._last_sent_class = None
        self._sedentary_streak_start_ts = 0.0
        self._persist_dedup_state()

    def to_dict(self) -> dict[str, Any]:
        seconds_since = (
            int(time.time() - self._last_motion_time)
            if self._last_motion_time is not None
            else None
        )
        last_key = self._last_sent_key
        return {
            "type": "motion",
            "connected": self._checker.ready(),
            "last_raw_actions": sorted(last_key[1]) if last_key else [],
            "last_user": last_key[0] if last_key else None,
            "buffered_snapshots": len(self._snapshots_buffer),
            "motion_detected": self._last_motion_time is not None,
            "seconds_since_motion": seconds_since,
        }
