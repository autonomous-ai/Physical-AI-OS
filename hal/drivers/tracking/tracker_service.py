"""Vision-guided object tracking with servo follow — gimbal hybrid mode."""

import logging
import math
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional, Tuple

import cv2
import numpy as np
import numpy.typing as npt

from hal import app_state
from hal.drivers.motors import hold
from hal.drivers.tracking import constants as C
from hal.safety.policy import cap_speed_dps
from hal.drivers.tracking.detection import ObjectDetector
from hal.drivers.tracking.filters import AlphaBetaFilter2D, PID, soft_deadband
from hal.drivers.tracking.servo_follow import ServoFollower
from hal.drivers.tracking.vit_tracker import (
    create_tracker,
    get_tracking_score,
    vit_init,
    vit_update,
)

logger = logging.getLogger(__name__)

MAX_TRACKING_RETRIES = 4

_TRACKING_MOTORS = ("base_yaw", "base_pitch", "elbow_pitch", "wrist_pitch")


def trust_window_s(detect_latency_s: float) -> float:
    """How long to go without a detector confirm before distrusting the tracker."""
    return max(
        C.TRUST_TRACKER_S,
        C.YOLO_REDETECT_S + 2.0 * detect_latency_s + C.TRUST_MARGIN_S,
    )


@dataclass
class TrackingState:
    """Mutable state for the active tracking session."""
    target_label: str = ""
    tracker: Optional[cv2.Tracker] = None
    bbox: Optional[Tuple[int, int, int, int]] = None
    confidence: Optional[float] = None
    low_confidence_frames: int = 0
    running: threading.Event = field(default_factory=threading.Event)
    thread: Optional[threading.Thread] = None


class TrackerService:
    """Manages a single object-tracking session with gimbal-style servo follow."""

    def __init__(self):
        self._state = TrackingState()
        self._lock = threading.Lock()
        # Serializes start() so two near-simultaneous /servo/track requests don't
        # both enter seeding (seconds, worst case) and end up spawning two
        # tracking threads that fight over the same servo state.
        self._start_lock = threading.Lock()
        self.last_error: str = ""
        self._yaw_pid = PID(C.PID_YAW_KP, C.PID_YAW_KI, C.PID_YAW_KD)
        self._pitch_pid = PID(C.PID_PITCH_KP, C.PID_PITCH_KI, C.PID_PITCH_KD)
        self._follower = ServoFollower()
        # Area (px²) of the last trusted bbox lock — baseline for bloat detection.
        self._track_init_area: float = 0.0
        self._detector = ObjectDetector(
            on_confidence=lambda c: setattr(self._state, "confidence", c)
        )

    @property
    def is_tracking(self) -> bool:
        return self._state.running.is_set()

    @property
    def status(self) -> dict:
        s = self._state
        return {
            "tracking": s.running.is_set(),
            "target": s.target_label or None,
            "bbox": list(s.bbox) if s.bbox else None,
            "confidence": s.confidence,
        }

    def detect_object(self, frame: npt.NDArray[np.uint8], target: str,
                      strict: bool = True,
                      allow_remote_fallback: bool = True) -> Optional[Tuple[int, int, int, int]]:
        """Detect an object by name — see detection.ObjectDetector.detect."""
        return self._detector.detect(frame, target, strict=strict,
                                     allow_remote_fallback=allow_remote_fallback)

    def start(
        self,
        bbox: Optional[Tuple[int, int, int, int]] = None,
        target_label="",
        camera_capture=None,
        animation_service=None,
    ) -> bool:
        """Start tracking an object."""
        if camera_capture is None or animation_service is None:
            self.last_error = "camera or animation service not available"
            logger.error("tracker start: %s", self.last_error)
            return False

        candidates = ([t for t in target_label if t]
                      if isinstance(target_label, (list, tuple))
                      else [target_label] if target_label else [])
        target_label = candidates[0] if candidates else ""

        # Serialize concurrent /servo/track calls.
        if not self._start_lock.acquire(blocking=False):
            self.last_error = "another tracking session is initializing — ignoring duplicate request"
            logger.warning("tracker start: %s target='%s'", self.last_error, target_label)
            return False
        try:
            self.stop()
            return self._start_locked(bbox, target_label, camera_capture,
                                      animation_service, candidates)
        finally:
            self._start_lock.release()

    def _start_locked(
        self,
        bbox: Optional[Tuple[int, int, int, int]],
        target_label: str,
        camera_capture,
        animation_service,
        candidates: Optional[list] = None,
    ) -> bool:
        """Body of start() — runs while _start_lock is held."""

        # Freeze servos so YOLO + tracker init see a sharp, stable frame.
        settle_s = 0.30
        t_req = time.perf_counter()
        animation_service.freeze()
        try:
            camera_capture.acquire_consumer()
            try:
                last_write = animation_service.last_servo_write
                quiet_from = (last_write + settle_s) if last_write else 0.0
                deadline = time.monotonic() + 1.5
                frame = None
                while time.monotonic() < deadline:
                    ts = camera_capture.last_frame_ts
                    if ts and ts >= quiet_from:
                        frame = camera_capture.last_frame
                        if frame is not None:
                            break
                    time.sleep(0.03)
                if frame is None:
                    frame = camera_capture.last_frame
            finally:
                camera_capture.release_consumer()
            t_after_settle = time.perf_counter()

            if frame is None:
                self.last_error = "no frame available from camera"
                logger.error("tracker start: %s", self.last_error)
                animation_service.unfreeze()
                return False
            frame = frame.copy()

            t_yolo_ms = 0.0
            if bbox is None:
                if not target_label:
                    self.last_error = "need either bbox or target label"
                    logger.error("tracker start: %s", self.last_error)
                    animation_service.unfreeze()
                    return False
                t_yolo0 = time.perf_counter()
                probe = list(candidates or [target_label])
                bbox = None
                best_conf = -1.0
                for allow_remote in (False, True):
                    for label in probe:
                        found = self.detect_object(frame, label,
                                                   allow_remote_fallback=allow_remote)
                        if found is None:
                            continue
                        conf = self._detector.last_confidence or 0.0
                        if conf > best_conf:
                            bbox, best_conf, target_label = found, conf, label
                    if bbox is not None:
                        break
                if bbox is not None and len(candidates or []) > 1:
                    logger.info("[track-start] chose '%s' conf=%.3f from candidates %s",
                                target_label, best_conf, candidates)
                t_yolo_ms = (time.perf_counter() - t_yolo0) * 1000
                if bbox is None:
                    self.last_error = f"'{target_label}' not found in frame"
                    logger.info("[track-start] settle=%.0fms yolo=%.0fms result=missed target='%s'",
                                (t_after_settle - t_req) * 1000, t_yolo_ms, target_label)
                    animation_service.unfreeze()
                    return False
        except Exception:
            animation_service.unfreeze()
            raise

        tracker = create_tracker()
        if tracker is None:
            logger.error("No OpenCV tracker available")
            animation_service.unfreeze()
            return False

        t_init0 = time.perf_counter()
        try:
            ok = vit_init(tracker, frame, bbox)
        except Exception as e:
            logger.error("tracker init exception for bbox %s: %s", bbox, e)
            animation_service.unfreeze()
            return False
        if ok is False:
            logger.error("tracker init failed for bbox %s", bbox)
            animation_service.unfreeze()
            return False
        t_init_ms = (time.perf_counter() - t_init0) * 1000
        t_total_ms = (time.perf_counter() - t_req) * 1000
        logger.info(
            "[track-start] settle=%.0fms yolo=%.0fms init=%.0fms total=%.0fms bbox=%s target='%s'",
            (t_after_settle - t_req) * 1000, t_yolo_ms, t_init_ms, t_total_ms, bbox, target_label,
        )

        with self._lock:
            self._state = TrackingState(
                target_label=target_label,
                tracker=tracker,
                bbox=bbox,
            )
            self._state.running.set()
            self._state.thread = threading.Thread(
                target=self._track_loop,
                args=(camera_capture, animation_service),
                daemon=True,
                name="servo-tracker",
            )
            self._state.thread.start()

        animation_service.unfreeze()
        animation_service.dispatch("play", "tracking")
        logger.info("Tracking started: '%s' bbox=%s — playing tracking animation", target_label, bbox)
        return True

    def stop(self):
        """Stop the current tracking session."""
        with self._lock:
            if not self._state.running.is_set():
                return
            self._state.running.clear()
            t = self._state.thread

        if t and t.is_alive():
            # Tracking loop iterations can take up to ~250ms (tracker update + frame
            # settle). 10s gives ~40 iterations of headroom so we never return while the
            # old thread is still racing the servo with a new session's commands.
            t.join(timeout=10.0)
            if t.is_alive():
                logger.error("[tracker.stop] previous tracking thread refused to exit after 10s")

        logger.info("Tracking stopped: '%s'", self._state.target_label)

    def update_bbox(self, bbox: Tuple[int, int, int, int], camera_capture=None) -> bool:
        """Re-init the active session's tracker to a caller-supplied bbox (x, y, w, h in
        original camera coords). Serves POST /servo/track/update.
        """
        if not self.is_tracking:
            return False
        if camera_capture is None:
            logger.warning("update_bbox: camera not available")
            return False
        frame = camera_capture.last_frame
        if frame is None:
            logger.warning("update_bbox: no frame available")
            return False
        tracker = create_tracker()
        if tracker is None:
            return False
        bbox = tuple(int(v) for v in bbox)
        try:
            if vit_init(tracker, frame, bbox) is False:
                logger.warning("update_bbox: tracker init failed for %s", bbox)
                return False
        except Exception as e:
            logger.warning("update_bbox: tracker init exception for %s: %s", bbox, e)
            return False
        state = self._state
        state.tracker = tracker
        state.bbox = bbox
        self._track_init_area = float(bbox[2] * bbox[3])
        logger.info("update_bbox: tracker re-initialized to %s", bbox)
        return True

    def _track_loop(self, camera_capture, animation_service):
        """Background loop: tracker update at FAST_LOOP_FPS + YOLO background correction."""
        state = self._state

        hold.claim(animation_service, hold.TRACKING)
        animation_service._tracking_active = True
        logger.info("Servo hold mode + tracking lock ON")

        try:
            with animation_service.bus_lock:
                # Always write the register, including zero (= unlimited), so
                # a prior return-to-zero or external command cannot leave a
                # stale hardware velocity cap on this tracking session.
                animation_service.robot.bus.sync_write(
                    "Goal_Velocity", {m: C.TRACKING_GOAL_VELOCITY for m in _TRACKING_MOTORS}
                )
                animation_service.robot.bus.sync_write(
                    "Acceleration", {m: C.TRACKING_ACCELERATION for m in _TRACKING_MOTORS}
                )
            logger.info("[tracking] Goal_Velocity=%d Acceleration=%d", C.TRACKING_GOAL_VELOCITY, C.TRACKING_ACCELERATION)
        except Exception as e:
            logger.warning("[tracking] Failed to set motor params: %s", e)

        # Read initial servo positions — the follower tracks them internally
        # after this. Reset PID state for a clean session.
        self._follower.read_initial_positions(animation_service)
        self._yaw_pid.reset()
        self._pitch_pid.reset()

        # Seed the servo goal with the current pose and start the follow worker
        # so it holds position until the vision loop publishes corrections.
        self._follower.seed_goal_current()
        self._follower.start(animation_service, state.running)

        # Baseline bbox area for bloat detection — the initial lock is trusted.
        self._track_init_area = float(state.bbox[2] * state.bbox[3]) if state.bbox else 0.0

        # Detection gating + reinit debounce state.
        recent_yolo_areas: list[float] = []
        last_reinit_t: float = 0.0

        ab_filter = AlphaBetaFilter2D(C.AB_ALPHA, C.AB_BETA, C.AB_GATE_PX)
        last_ab_t: Optional[float] = None
        prev_dx: Optional[float] = None
        prev_dy: Optional[float] = None
        motion_state = "INIT"
        last_servo_t: float = 0.0         # timestamp of last servo fire (for cooldown)
        miss_count = 0
        yolo_miss_count = 0
        low_conf_window: deque = deque(maxlen=C.LOW_CONF_WINDOW)
        saccade_mode = False
        retry_count = 0
        frame_count = 0
        t_csrt_acc = 0.0
        servo_count = 0    # frames where servo actually fired
        track_start_t = time.perf_counter()
        last_yolo_t = track_start_t
        # Detector-gated trust: skip servo if YOLO hasn't confirmed target recently.
        last_yolo_confirm_t = track_start_t
        detect_latency_s = 0.0
        fps_t0 = track_start_t

        yolo_q: queue.Queue = queue.Queue(maxsize=1)
        yolo_running = threading.Event()

        def _do_retry() -> bool:
            """Try YOLO on a fresh frame, reinit tracker. Returns True to continue."""
            nonlocal retry_count, miss_count, yolo_miss_count
            nonlocal prev_dx, prev_dy, motion_state, last_yolo_t
            retry_count += 1
            if retry_count > MAX_TRACKING_RETRIES:
                logger.warning("[retry] exhausted %d retries, stopping", MAX_TRACKING_RETRIES)
                return False
            logger.info("[retry] attempt %d/%d (soft)", retry_count, MAX_TRACKING_RETRIES)
            self._yaw_pid.reset()
            self._pitch_pid.reset()
            _f = camera_capture.last_frame
            if _f is not None:
                _bbox = self.detect_object(_f, state.target_label, strict=False)
                if _bbox is not None:
                    _t = create_tracker()
                    if _t is not None:
                        try:
                            if vit_init(_t, _f, _bbox) is not False:
                                state.tracker = _t
                                state.bbox = _bbox
                                self._track_init_area = float(_bbox[2] * _bbox[3])
                                logger.info("[retry] tracker reinit OK bbox=%s", _bbox)
                        except Exception as _e:
                            logger.warning("[retry] tracker init failed: %s", _e)
            miss_count = 0
            yolo_miss_count = 0
            low_conf_window.clear()   # fresh lock → stale conf history is meaningless
            state.low_confidence_frames = 0
            ab_filter.reset()
            prev_dx = prev_dy = None
            motion_state = "INIT"
            last_yolo_t = 0
            while True:
                try: yolo_q.get_nowait()
                except queue.Empty: break
            return True

        def _fire_yolo(frame_snap: npt.NDArray[np.uint8]) -> None:
            nonlocal detect_latency_s
            t0_yolo = time.perf_counter()
            # No remote fallback on a routine redetect: this call confirms a lock we
            # already have, and a remote round-trip would stall the single-flight detect
            # thread long enough to trip the trust gate below.
            result = self.detect_object(frame_snap, state.target_label, strict=False,
                                        allow_remote_fallback=False)
            t_yolo_ms = (time.perf_counter() - t0_yolo) * 1000
            detect_latency_s = (0.7 * detect_latency_s + 0.3 * (t_yolo_ms / 1000.0)
                                if detect_latency_s > 0 else t_yolo_ms / 1000.0)
            logger.info("[yolo-bg] detect=%.0fms result=%s bbox=%s target='%s'",
                        t_yolo_ms, "found" if result is not None else "missed", result, state.target_label)
            if result is None:
                logger.info("[tracking_yolo_response] target='%s' found=False latency=%.0fms", state.target_label, t_yolo_ms)
            try:
                yolo_q.put_nowait(result)
            except queue.Full:
                pass
            finally:
                yolo_running.clear()

        try:
            while state.running.is_set():
                t0 = time.perf_counter()

                # This is a wall-clock session limit. Check before reading the
                # frame so a stalled camera cannot keep tracking alive forever.
                if time.perf_counter() - track_start_t > C.MAX_TRACK_DURATION_S:
                    logger.warning("Tracking timeout after %ds, stopping", C.MAX_TRACK_DURATION_S)
                    break

                frame = camera_capture.last_frame
                if frame is None:
                    time.sleep(1.0 / C.FAST_LOOP_FPS)
                    continue

                h_fr, w_fr = frame.shape[:2]
                t_csrt0 = time.perf_counter()
                ok, new_bbox = vit_update(state.tracker, frame)
                t_csrt_ms = (time.perf_counter() - t_csrt0) * 1000
                t_csrt_acc += t_csrt_ms

                # Confidence-based ghost-lock detection (ViT only) — sliding
                # window, so a single above-threshold flicker no longer resets
                # the count and keeps a ghost lock alive.
                confidence = get_tracking_score(state.tracker)
                state.confidence = confidence
                if ok:
                    low_conf_window.append(1 if confidence < C.CONFIDENCE_THRESHOLD else 0)
                    state.low_confidence_frames = sum(low_conf_window)
                    if state.low_confidence_frames >= C.LOW_CONF_STOP_COUNT:
                        logger.warning("Tracker lost '%s' (conf=%.3f, %d/%d low in last %d frames) — stopping",
                                       state.target_label, confidence,
                                       state.low_confidence_frames, C.LOW_CONF_STOP_COUNT,
                                       len(low_conf_window))
                        break
                    if confidence < C.CONFIDENCE_THRESHOLD:
                        logger.info("[conf] low %.3f (%d/%d in window) target='%s'",
                                    confidence, state.low_confidence_frames,
                                    C.LOW_CONF_STOP_COUNT, state.target_label)
                        # Don't glide on toward the stale goal while skipping.
                        self._follower.hold()
                        time.sleep(1.0 / C.FAST_LOOP_FPS)
                        continue

                if not ok:
                    miss_count += 1
                    logger.info("[search] tracker miss %d/%d target='%s'", miss_count, C.YOLO_MAX_MISS, state.target_label)
                    if miss_count == 1:
                        last_yolo_t = 0
                    coast_speed = (ab_filter.vx ** 2 + ab_filter.vy ** 2) ** 0.5
                    if miss_count <= C.MISS_COAST_FRAMES and coast_speed > C.VFF_MOVING_MIN_PXS:
                        dt_c = 1.0 / C.FAST_LOOP_FPS
                        deg_per_px = C.CAMERA_FOV_DEG / w_fr
                        _lim = C.PID_OUTPUT_MAX_DEG
                        self._follower.command_pid(
                            max(-_lim, min(_lim, C.VFF_GAIN * ab_filter.vx * deg_per_px * dt_c)),
                            max(-_lim, min(_lim, C.VFF_GAIN * ab_filter.vy * deg_per_px * dt_c)),
                        )
                        motion_state = "COAST"
                        logger.info("[coast] miss %d — panning along v=(%.0f,%.0f)px/s",
                                    miss_count, ab_filter.vx, ab_filter.vy)
                        time.sleep(1.0 / C.FAST_LOOP_FPS)
                        continue
                    # Sweep base_yaw to search for object — alternates direction every 8 frames.
                    # Route through the servo goal so the follow worker drives it (one owner).
                    _sweep_dir = 1 if ((miss_count - 1) // 8) % 2 == 0 else -1
                    self._follower.sweep_yaw(2.0 * _sweep_dir)
                    if miss_count >= C.YOLO_MAX_MISS:
                        if _do_retry():
                            continue
                        break
                    time.sleep(1.0 / C.FAST_LOOP_FPS)
                    continue

                miss_count = 0
                state.bbox = tuple(int(v) for v in new_bbox)
                bx, by, bw, bh = state.bbox

                frame_area = float(h_fr * w_fr)
                bbox_ratio = (bw * bh) / frame_area
                # "Object too close" stop removed intentionally — servo PID drives off
                # the centroid, not bbox size, so a person filling the frame can still
                # be tracked.

                # Ghost-lock: bbox shrunk to a sliver (typically locked on frame edge).
                if bbox_ratio < C.DETECT_MIN_AREA_RATIO:
                    logger.warning("[bbox] ghost-lock: %dx%d area=%.2f%% — stopping",
                                   bw, bh, bbox_ratio * 100)
                    break

                # Bbox drifted large — fire YOLO to correct, but DON'T skip the frame.
                if bbox_ratio > C.DETECT_MAX_AREA_RATIO:
                    logger.warning("[bbox] large (%.1f%% > %.1f%%) — firing YOLO bg, keep tracking",
                                   bbox_ratio * 100, C.DETECT_MAX_AREA_RATIO * 100)
                    if not yolo_running.is_set() and state.target_label:
                        yolo_running.set()
                        snap = frame.copy()
                        threading.Thread(
                            target=_fire_yolo, args=(snap,), daemon=True, name="yolo-worker"
                        ).start()

                cx_obj = bx + bw / 2.0
                cy_obj = by + bh / 2.0

                now_ab = time.perf_counter()
                ab_dt = 0.0 if last_ab_t is None else (now_ab - last_ab_t)
                last_ab_t = now_ab
                fx, fy, vx_f, vy_f, ab_gated = ab_filter.update(cx_obj, cy_obj, ab_dt)
                lead_x = fx + vx_f * C.AB_LEAD_S
                lead_y = fy + vy_f * C.AB_LEAD_S
                dx = float(lead_x - w_fr / 2.0)
                dy = float(lead_y - h_fr / 2.0)
                if ab_gated:
                    logger.debug("[ab-gate] meas=(%.0f,%.0f) coast→(%.0f,%.0f) v=(%.0f,%.0f)px/s",
                                 cx_obj, cy_obj, fx, fy, vx_f, vy_f)

                offset_mag = (dx ** 2 + dy ** 2) ** 0.5
                screen_x_pct = (cx_obj / w_fr) * 100
                screen_y_pct = (cy_obj / h_fr) * 100
                quadrant = ("TOP" if dy < 0 else "BOT") + "_" + ("LEFT" if dx < 0 else "RIGHT")
                if prev_dx is not None and prev_dy is not None:
                    ddx, ddy = dx - prev_dx, dy - prev_dy
                    if (ddx ** 2 + ddy ** 2) ** 0.5 > 2:
                        angle = ["→", "↗", "↑", "↖", "←", "↙", "↓", "↘"]
                        sector = int((math.degrees(math.atan2(-ddy, ddx)) + 180 + 22.5) / 45) % 8
                        direction = angle[sector]
                    else:
                        direction = "·"
                    moving_str = motion_state
                else:
                    direction, moving_str = "·", "INIT"
                logger.debug("[tracking_object] target='%s' pos=(%.0f%%,%.0f%%) quad=%s offset=(%.0f,%.0f) dist=%.0fpx state=%s dir=%s bbox_area=%.1f%% conf=%.2f yolo_age=%.1fs",
                            state.target_label, screen_x_pct, screen_y_pct, quadrant,
                            dx, dy, offset_mag, moving_str, direction, bbox_ratio * 100,
                            confidence, time.perf_counter() - last_yolo_confirm_t)

                now_t = time.perf_counter()
                if saccade_mode:
                    if offset_mag < C.SACCADE_EXIT_FRAC * w_fr:
                        saccade_mode = False
                elif offset_mag > C.SACCADE_OFFSET_FRAC * w_fr:
                    saccade_mode = True
                # The tracking loop is a declared-bound path like any other: its own
                # pursuit/saccade ceilings (55/100 deg/s) are tuning, not permission.
                self._follower.set_profile(
                    C.SACCADE_SMOOTH_TIME if saccade_mode else C.SERVO_SMOOTH_TIME,
                    cap_speed_dps(
                        app_state.safety_policy,
                        C.SACCADE_MAX_SPEED_DPS if saccade_mode else C.SERVO_MAX_SPEED_DPS,
                    ),
                )
                err_dx = soft_deadband(dx, w_fr * C.DEAD_ZONE_INNER_PCT,
                                       w_fr * C.DEAD_ZONE_YAW_PCT, C.DEAD_ZONE_CREEP_GAIN)
                err_dy = soft_deadband(dy, h_fr * C.DEAD_ZONE_INNER_PCT,
                                       h_fr * C.DEAD_ZONE_PITCH_PCT, C.DEAD_ZONE_CREEP_GAIN)
                speed_pxs = (vx_f ** 2 + vy_f ** 2) ** 0.5
                moving_ff = speed_pxs > C.VFF_MOVING_MIN_PXS
                centered = err_dx == 0.0 and err_dy == 0.0
                yolo_age = now_t - last_yolo_confirm_t
                trust_window = trust_window_s(detect_latency_s)
                # Bbox-trust guard: ViT bloated past its last trusted lock (or the
                # absolute ceiling) → centroid is garbage.
                cur_area_px = bw * bh
                bloated_vs_trust = (
                    self._track_init_area > 0
                    and cur_area_px > self._track_init_area * C.BLOAT_HOLD_MULT
                )
                bbox_untrusted = bbox_ratio >= C.BBOX_FREEZE_RATIO or bloated_vs_trust
                # Ghost-lock recovery: ViT sometimes reports ok=True with a bbox larger
                # than the frame (lock dissolved into background).
                if bbox_ratio > 0.95 and yolo_age >= 3.0:
                    logger.warning("[ghost-lock] bbox=%.0f%% no-detect=%.1fs → forced retry",
                                   bbox_ratio * 100, yolo_age)
                    if _do_retry():
                        last_yolo_confirm_t = time.perf_counter()
                        continue
                    break
                if yolo_age >= C.STOP_NO_YOLO_S:
                    logger.warning("[yolo-trust] no YOLO confirm for %.1fs > %.1fs — stopping ghost",
                                   yolo_age, C.STOP_NO_YOLO_S)
                    break
                elif bbox_untrusted:
                    self._yaw_pid.reset()
                    self._pitch_pid.reset()
                    motion_state = "BLOAT-HOLD"
                    self._follower.hold()
                    if not yolo_running.is_set() and state.target_label:
                        last_yolo_t = 0
                    logger.info("[bbox] untrusted (%s): area=%.0f%% cur_px=%.0f trust_px=%.0f — HOLD servo, await YOLO relock",
                                "overflow" if bbox_ratio >= C.BBOX_FREEZE_RATIO else "bloat>%.1fx" % C.BLOAT_HOLD_MULT,
                                bbox_ratio * 100, cur_area_px, self._track_init_area)
                elif centered and not moving_ff:
                    # Truly centered AND still — hold and let the integral clear.
                    # (A centered but MOVING target falls through to keep panning
                    # on feedforward so it never drifts out before the PID reacts.)
                    self._yaw_pid.reset()
                    self._pitch_pid.reset()
                    self._follower.hold()
                    motion_state = "CENTERED"
                elif confidence < C.SERVO_MIN_CONF:
                    # Tracker barely holding the lock — don't chase, even with a fresh
                    # detector confirm (conf 0.15–0.4 + confirm used to be a blind zone
                    # that kept the servo hunting ghosts).
                    self._yaw_pid.reset()
                    self._pitch_pid.reset()
                    motion_state = "LOW-CONF-HOLD"
                    self._follower.hold()
                elif yolo_age >= trust_window and confidence < C.TRACKER_TRUST_CONF:
                    # Tracker AND detector both unsure — hold servo, don't chase
                    # phantom.
                    motion_state = "WAIT-YOLO"
                    self._follower.hold()
                elif (now_t - last_servo_t) >= C.SERVO_COOLDOWN_S:
                    motion_state = "SACCADE" if saccade_mode else "CHASING"
                    # Position PID on the soft-deadbanded error. Yaw sign: dx>0
                    # (object on right) → base_yaw must INCREASE to chase right
                    # (verified empirically vs legacy gimbal path).
                    yaw_pid = self._yaw_pid.update(err_dx)
                    pitch_pid = self._pitch_pid.update(err_dy)
                    dt_fire = (min(C.VFF_MAX_DT_S, now_t - last_servo_t)
                               if last_servo_t > 0 else 1.0 / C.FAST_LOOP_FPS)
                    deg_per_px = C.CAMERA_FOV_DEG / w_fr
                    yaw_ff = C.VFF_GAIN * vx_f * deg_per_px * dt_fire
                    pitch_ff = C.VFF_GAIN * vy_f * deg_per_px * dt_fire
                    # Combine and clamp to the PID output limit so ff + pid can't
                    # exceed the per-fire travel cap.
                    _lim = C.PID_OUTPUT_MAX_DEG
                    yaw_step = max(-_lim, min(_lim, yaw_pid + yaw_ff))
                    pitch_correction = max(-_lim, min(_lim, pitch_pid + pitch_ff))
                    logger.info("[pid-fire] offset=(%.0f,%.0f) v=(%.0f,%.0f)px/s → yaw=%.2f(ff%.2f) pitch=%.2f(ff%.2f) target='%s'",
                                dx, dy, vx_f, vy_f,
                                yaw_step, yaw_ff, pitch_correction, pitch_ff, state.target_label)
                    self._follower.command_pid(yaw_step, pitch_correction)
                    servo_count += 1
                    last_servo_t = now_t
                prev_dx, prev_dy = dx, dy

                # Drain YOLO result queue — re-init tracker ONLY when it has clearly
                # diverged.
                try:
                    yolo_bbox = yolo_q.get_nowait()
                    if yolo_bbox is not None:
                        miss_count = 0
                        last_yolo_confirm_t = time.perf_counter()
                        cur_bbox = state.bbox
                        cur_area = (cur_bbox[2] * cur_bbox[3]) if cur_bbox else 0
                        yolo_area = yolo_bbox[2] * yolo_bbox[3]
                        # Detection gate (outlier rejection): reject a box whose area is
                        # wildly off the recent median — a false detection the tracker
                        # must NOT reinit to.
                        recent_yolo_areas.append(float(yolo_area))
                        if len(recent_yolo_areas) > 5:
                            recent_yolo_areas.pop(0)
                        area_outlier = False
                        med = 0.0
                        if len(recent_yolo_areas) >= 3:
                            med = sorted(recent_yolo_areas)[len(recent_yolo_areas) // 2]
                            if med > 0 and (yolo_area > med * C.YOLO_AREA_GATE_MULT
                                            or yolo_area < med / C.YOLO_AREA_GATE_MULT):
                                area_outlier = True
                        cdx = cdy = 0.0
                        if cur_bbox is not None:
                            cdx = (cur_bbox[0] + cur_bbox[2] / 2.0) - (yolo_bbox[0] + yolo_bbox[2] / 2.0)
                            cdy = (cur_bbox[1] + cur_bbox[3] / 2.0) - (yolo_bbox[1] + yolo_bbox[3] / 2.0)
                        center_dist = (cdx ** 2 + cdy ** 2) ** 0.5
                        cur_min_dim = min(cur_bbox[2], cur_bbox[3]) if cur_bbox else 0
                        diverge_threshold = max(120.0, cur_min_dim * 0.4)
                        bloated = cur_area > 0 and cur_area > yolo_area * 2.0
                        diverged = center_dist > diverge_threshold
                        # Reinit debounce: rate-limit reinits so a noisy detector can't
                        # reinit every frame (the churn that whipsaws the servo).
                        now_reinit = time.perf_counter()
                        frame_diag = (w_fr ** 2 + h_fr ** 2) ** 0.5
                        clearly_lost = center_dist > frame_diag * C.LOST_CENTER_FRAC
                        cooldown_ok = (now_reinit - last_reinit_t) >= C.REINIT_COOLDOWN_S
                        if area_outlier:
                            logger.info("[detect-gate] reject YOLO area=%d (median=%d, gate=%.0fx) "
                                        "— keep current lock", yolo_area, int(med), C.YOLO_AREA_GATE_MULT)
                        elif (bloated or diverged) and (cooldown_ok or clearly_lost):
                            logger.info("[drift-correct] reinit reason: bloated=%s diverged=%s lost=%s "
                                        "cur_area=%d yolo_area=%d center_dist=%.0fpx",
                                        bloated, diverged, clearly_lost, cur_area, yolo_area, center_dist)
                            ab_filter.reset()
                            new_tracker = create_tracker()
                            if new_tracker is not None:
                                reinit_frame = camera_capture.last_frame
                                if reinit_frame is not None:
                                    try:
                                        ok_r = vit_init(new_tracker, reinit_frame, yolo_bbox)
                                        if ok_r is not False:
                                            state.tracker = new_tracker
                                            state.bbox = yolo_bbox
                                            self._track_init_area = float(yolo_bbox[2] * yolo_bbox[3])
                                            last_reinit_t = now_reinit
                                            motion_state = "INIT"
                                    except Exception as e:
                                        logger.warning("YOLO re-init failed: %s", e)
                        else:
                            logger.debug("[drift-correct] tracker OK / debounced "
                                         "(bloated=%s diverged=%s cooldown_ok=%s "
                                         "cur_area=%d yolo_area=%d center_dist=%.0fpx)",
                                         bloated, diverged, cooldown_ok,
                                         cur_area, yolo_area, center_dist)
                    else:
                        yolo_miss_count += 1
                        logger.debug("YOLO scan: target not found (%d consecutive)", yolo_miss_count)
                except queue.Empty:
                    pass
                else:
                    if yolo_bbox is not None:
                        yolo_miss_count = 0

                # Force immediate YOLO redetect when object drifts to frame edge —
                # the tracker will lose lock before the normal interval fires.
                if (abs(dx) > w_fr * 0.25 or abs(dy) > h_fr * 0.25) and not yolo_running.is_set():
                    last_yolo_t = 0
                    logger.info("[edge] offset=(%.0f,%.0f) > 25%% frame → force YOLO target='%s'",
                                dx, dy, state.target_label)

                now = time.perf_counter()
                if state.target_label and not yolo_running.is_set() and now - last_yolo_t >= C.YOLO_REDETECT_S:
                    last_yolo_t = now
                    yolo_running.set()
                    snap = frame.copy()
                    threading.Thread(
                        target=_fire_yolo, args=(snap,), daemon=True, name="yolo-worker"
                    ).start()

                frame_count += 1
                fps_elapsed = time.perf_counter() - fps_t0
                if fps_elapsed >= 2.0:
                    csrt_avg = t_csrt_acc / frame_count if frame_count else 0.0
                    frame_avg = fps_elapsed * 1000 / frame_count if frame_count else 0.0
                    logger.info(
                        "[track-loop] fps=%.1f tracker=%.0fms servo_fires=%d frame=%.0fms"
                        " offset=(%.0f,%.0f) bbox=%s target='%s'",
                        frame_count / fps_elapsed,
                        csrt_avg, servo_count,
                        frame_avg, dx, dy, state.bbox, state.target_label,
                    )
                    try:
                        import subprocess as _sp
                        cpu = float(open("/proc/loadavg").read().split()[0])
                        mem_info = open("/proc/meminfo").read()
                        mem_total = int(next(l.split()[1] for l in mem_info.splitlines() if "MemTotal" in l))
                        mem_avail = int(next(l.split()[1] for l in mem_info.splitlines() if "MemAvailable" in l))
                        mem_used_pct = (mem_total - mem_avail) / mem_total * 100
                        volt = _sp.check_output(["vcgencmd", "measure_volts", "core"],
                                                stderr=_sp.DEVNULL, text=True).strip()
                        logger.info("[tracking_system] cpu_load1=%.2f ram_used=%.0f%% voltage=%s", cpu, mem_used_pct, volt)
                    except Exception:
                        pass
                    frame_count = 0
                    t_csrt_acc = 0.0
                    servo_count = 0
                    fps_t0 = time.perf_counter()

                dt = time.perf_counter() - t0
                sleep_time = (1.0 / C.FAST_LOOP_FPS) - dt
                if sleep_time > 0:
                    time.sleep(sleep_time)

        finally:
            animation_service._tracking_active = False
            hold.release(animation_service, hold.TRACKING)
            state.running.clear()

            self._follower.join(timeout=2.0)
            self._follower.clear_goal()

            try:
                # Idle must interpolate from the physical pose where tracking stopped.
                current = animation_service.get_positions()
                if current:
                    animation_service._current_state = dict(current)
                with animation_service.bus_lock:
                    animation_service.robot.bus.sync_write(
                        "Goal_Velocity", {m: 0 for m in _TRACKING_MOTORS}
                    )
                    animation_service.robot.bus.sync_write(
                        "Acceleration", {m: 254 for m in _TRACKING_MOTORS}
                    )
                logger.info("Tracking ended — idle resuming from current pose")
            except Exception as e:
                logger.warning("Tracking ended — failed to seed idle pose: %s", e)

            if not animation_service._running.is_set():
                animation_service._running.set()
                animation_service._event_thread = threading.Thread(
                    target=animation_service._event_loop, daemon=True
                )
                animation_service._event_thread.start()

            # Restart idle. The tracking lock in _continue_playback cleared
            # _current_recording, so the revived event loop has nothing to play and
            # would return at its first guard forever — arm rigid with torque on.
            # Unless another owner still holds the body: idle would play once, then
            # freeze at its last frame. The arm stays where tracking left it.
            held = hold.holder(animation_service)
            if held:
                logger.info("Tracking ended — servo still held by %s, idle not resumed", held)
            else:
                animation_service.dispatch("play", animation_service.idle_recording)
