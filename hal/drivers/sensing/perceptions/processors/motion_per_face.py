"""Per-face action recognition."""

import logging
import threading
import time
from copy import copy
from typing import Any, override

import cv2

import hal.config as config
from hal.drivers.sensing.perceptions.models import FaceDetectionData
from hal.drivers.sensing.perceptions.processors.base import Perception
from hal.drivers.sensing.perceptions.processors.motion import (
    ACTIVITY_GROUP,
    MotionDetection,
    RemoteMotionChecker,
    coarse_classes,
)
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenceState, PresenseService

logger = logging.getLogger(__name__)


class _FaceSession:
    """Per-face state: WS session + action buffer + per-action dedup."""

    def __init__(
        self,
        face_id: str,
        checker: RemoteMotionChecker,
        dedup_window_s: float = 300.0,
        min_frames: int = 4,
    ):
        self.face_id: str = face_id
        self.checker: RemoteMotionChecker = checker
        self.last_seen: float = time.time()
        self.frames_received: int = 0
        self.actions_buffer: list[str] = []
        self.snapshots_buffer: list[cv2.typing.MatLike] = []
        self._sent_actions: dict[str, float] = {}
        self.dedup_window_s: float = dedup_window_s
        self._min_frames: int = min_frames

    def update(self, crop: cv2.typing.MatLike) -> list[MotionDetection] | None:
        """Send a crop to the backend and buffer results. Tracks frame count."""
        self.last_seen = time.time()
        self.frames_received += 1
        detections = self.checker.update(crop)
        if detections:
            self.actions_buffer.extend([d.class_name for d in detections])
            self.snapshots_buffer.append(crop)
        return detections

    @property
    def sufficient_frames(self) -> bool:
        """True once enough frames have been received for reliable classification."""
        return self.frames_received >= self._min_frames

    def filter_new_actions(self, labels: set[str], now: float) -> set[str]:
        """Return only labels that haven't been sent within the dedup window."""
        cutoff = now - self.dedup_window_s
        self._sent_actions = {
            k: ts for k, ts in self._sent_actions.items() if ts >= cutoff
        }

        return {label for label in labels if label not in self._sent_actions}

    def mark_sent(self, labels: set[str], now: float) -> None:
        for label in labels:
            self._sent_actions[label] = now


class MotionPerFacePerception(Perception[FaceDetectionData]):
    """Per-face action recognition via the remote DL backend.

    Each face_id has its own backend session so frame buffers don't mix between people.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        base_url: str = config.DL_MOTION_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
        flush_interval: float = config.MOTION_FLUSH_S,
        dedup_window_s: float = config.MOTION_PER_FACE_DEDUP_WINDOW_S,
        session_ttl_s: float = config.MOTION_PER_FACE_SESSION_TTL_S,
        min_frames: int = config.MOTION_PER_FACE_MIN_FRAMES,
    ):
        super().__init__(perception_state, send_event)
        self._presense_service: PresenseService | None = presense_service
        self._base_url: str = base_url
        self._api_key: str = api_key
        self._flush_interval: float = flush_interval
        self._dedup_window_s: float = dedup_window_s
        self._session_ttl_s: float = session_ttl_s
        self._min_frames: int = min_frames
        self._whitelist: list[str] | None = self._load_whitelist()

        self._sessions: dict[str, _FaceSession] = {}
        self._lock: threading.RLock = threading.RLock()
        self._last_flush_ts: float = 0.0

        # Global (cross-face) cooldown floor, mirroring MotionPerception's. NOTE: this
        # perception is an ALTERNATIVE to MotionPerception (both emit motion.activity);
        # when both are enabled their floors are independent — don't run both.
        self._event_cooldown_s: float = config.MOTION_EVENT_COOLDOWN_S
        self._transition_min_gap_s: float = config.MOTION_TRANSITION_MIN_GAP_S
        self._last_event_ts: float = 0.0
        self._last_sent_class: frozenset[str] | None = None
        self._last_sent_user: str = ""

    @staticmethod
    def _load_whitelist() -> list[str] | None:
        from pathlib import Path

        whitelist_path = Path(__file__).parent / "resources" / "white_list.txt"
        if not whitelist_path.exists():
            return None
        lines = whitelist_path.read_text().strip().splitlines()
        return [line.strip() for line in lines if line.strip()] or None

    def _get_or_create_session(self, face_id: str) -> _FaceSession:
        """Get existing session for face_id or create a new one with its own WS connection."""
        if face_id in self._sessions:
            return self._sessions[face_id]

        logger.info("[motion_per_face] creating WS session for face '%s'", face_id)
        checker = RemoteMotionChecker(
            base_url=self._base_url,
            api_key=self._api_key,
            whitelist=self._whitelist,
            threshold=config.MOTION_CONFIDENCE_THRESHOLD,
            person_detection_enabled=False,
        )
        session = _FaceSession(
            face_id=face_id,
            checker=checker,
            dedup_window_s=self._dedup_window_s,
            min_frames=self._min_frames,
        )
        self._sessions[face_id] = session
        return session

    def _evict_stale_sessions(self) -> None:
        """Remove sessions that haven't been updated within the TTL."""
        now = time.time()
        stale = [
            fid
            for fid, s in self._sessions.items()
            if now - s.last_seen > self._session_ttl_s
        ]
        for fid in stale:
            session = self._sessions.pop(fid)
            logger.info("[motion_per_face] evicting stale session for '%s'", fid)
            try:
                session.checker.close()
            except Exception:
                pass

    @staticmethod
    def _expand_face_bbox(
        bbox: list[int],
        frame_h: int,
        frame_w: int,
    ) -> tuple[int, int, int, int]:
        """Expand face bbox: 1x the face height upward, 2x on left/right/down.

        Args:
            bbox: [x1, y1, x2, y2] from face detection.
            frame_h: Frame height.
            frame_w: Frame width.

        Returns:
            (x1, y1, x2, y2) clamped to frame bounds.
        """
        x1, y1, x2, y2 = bbox
        face_w = x2 - x1
        face_h = y2 - y1

        new_x1 = x1 - face_w * 2
        new_y1 = y1 - face_h * 1
        new_x2 = x2 + face_w * 2
        new_y2 = y2 + face_h * 2

        new_x1 = max(0, int(new_x1))
        new_y1 = max(0, int(new_y1))
        new_x2 = min(frame_w, int(new_x2))
        new_y2 = min(frame_h, int(new_y2))

        return new_x1, new_y1, new_x2, new_y2

    @override
    def to_dict(self) -> dict[str, Any]:
        with self._lock:
            return {
                "type": "motion_per_face",
                "active_sessions": len(self._sessions),
                "tracked_faces": list(self._sessions.keys()),
            }

    @override
    def cleanup(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                try:
                    session.checker.close()
                except Exception:
                    pass
            self._sessions.clear()

    def reset_dedup(self, new_user: str = "") -> None:
        """Clear the global cooldown floor only if the visible user actually changed.

        collapses to "unknown" and must not reopen the floor on every presence.enter).
        """
        with self._lock:
            if self._last_sent_class is None:
                return
            if self._last_sent_user == new_user:
                logger.debug(
                    "[motion_per_face] floor reset skipped — same user %r",
                    new_user,
                )
                return
            logger.info(
                "[motion_per_face] floor reset (user %r → %r)",
                self._last_sent_user,
                new_user,
            )
            self._last_event_ts = 0.0
            self._last_sent_class = None
            self._last_sent_user = ""

    @override
    def _check_impl(self, data: FaceDetectionData) -> None:
        if data.frame is None or not data.faces:
            logger.debug(
                "[motion_per_face] skipping: frame=%s faces=%d",
                "None" if data.frame is None else "ok",
                len(data.faces) if data.faces else 0,
            )
            return
        logger.debug("[motion_per_face] processing %d face(s)", len(data.faces))

        frame = data.frame
        frame_h, frame_w = frame.shape[:2]

        with self._lock:
            self._evict_stale_sessions()

            for face in data.faces:
                face_id = face.person_id
                session = self._get_or_create_session(face_id)

                ex1, ey1, ex2, ey2 = self._expand_face_bbox(
                    face.bbox,
                    frame_h,
                    frame_w,
                )
                if ex2 <= ex1 or ey2 <= ey1:
                    continue

                crop = frame[ey1:ey2, ex1:ex2]
                if crop.size == 0:
                    continue

                try:
                    session.update(crop)
                except Exception:
                    logger.exception(
                        "[motion_per_face] inference error for '%s'", face_id
                    )
                    continue

            self._flush_all()

    def _flush_all(self) -> None:
        """Flush action buffers for all face sessions if the flush interval elapsed."""
        cur_ts = time.time()
        if (cur_ts - self._last_flush_ts) < self._flush_interval:
            return
        self._last_flush_ts = cur_ts

        for session in self._sessions.values():
            if not session.actions_buffer:
                continue

            if not session.sufficient_frames:
                logger.debug(
                    "[motion_per_face] '%s' not ready yet (%d/%d frames)",
                    session.face_id,
                    session.frames_received,
                    session._min_frames,
                )
                continue

            actions = copy(session.actions_buffer)
            snapshots = copy(session.snapshots_buffer)
            session.actions_buffer.clear()
            session.snapshots_buffer.clear()

            labels: set[str] = set()
            for a in reversed(actions):
                group = ACTIVITY_GROUP.get(a)
                if group is None:
                    continue
                if group == "emotional":
                    continue
                if group in ("sedentary", "tired"):
                    labels.add(a)
                else:
                    labels.add(group)

            if not labels:
                continue

            if (
                self._presense_service is not None
                and self._presense_service.state != PresenceState.PRESENT
            ):
                continue

            classes: frozenset[str] = coarse_classes(labels)
            class_changed: bool = (
                self._last_sent_class is not None
                and classes != self._last_sent_class
            )
            transition_bypass: bool = class_changed and (
                (cur_ts - self._last_event_ts) >= self._transition_min_gap_s
            )
            if (
                not transition_bypass
                and self._last_sent_class is not None
                and (cur_ts - self._last_event_ts) < self._event_cooldown_s
            ):
                logger.info(
                    "[motion_per_face] cooldown drop for '%s': %s "
                    "(last event %.1fs ago < %.0fs floor, class %s)",
                    session.face_id,
                    sorted(labels),
                    cur_ts - self._last_event_ts,
                    self._event_cooldown_s,
                    "changed but < %.0fs transition gap"
                    % self._transition_min_gap_s
                    if class_changed
                    else "unchanged",
                )
                continue
            if transition_bypass:
                logger.info(
                    "[motion_per_face] transition bypass: %s → %s "
                    "(last event %.1fs ago)",
                    sorted(self._last_sent_class or ()),
                    sorted(classes),
                    cur_ts - self._last_event_ts,
                )

            new_labels = session.filter_new_actions(labels, cur_ts)
            if not new_labels:
                logger.info(
                    "[motion_per_face] dedup drop all for '%s': %s (all seen within %.0fs)",
                    session.face_id,
                    sorted(labels),
                    session.dedup_window_s,
                )
                continue
            if new_labels != labels:
                logger.info(
                    "[motion_per_face] dedup partial for '%s': sending %s, suppressed %s",
                    session.face_id,
                    sorted(new_labels),
                    sorted(labels - new_labels),
                )
            session.mark_sent(new_labels, cur_ts)
            labels = new_labels

            self._last_event_ts = cur_ts
            self._last_sent_class = classes
            self._last_sent_user = self._perception_state.current_user.data or ""

            message = (
                f"Activity detected ({session.face_id}): {', '.join(sorted(labels))}."
            )
            logger.info(
                "[motion_per_face] flushing for '%s': %s", session.face_id, message
            )

            snapshot = snapshots[-1] if snapshots else None
            self._send_event(
                "motion.activity",
                message,
                "motion_activity",
                [snapshot] if snapshot is not None else [],
                None,
            )
