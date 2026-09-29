"""Pose estimation + ergonomic sampling via perception-service WS."""

import base64
import json
import logging
import os
import shutil
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, override

import cv2
import numpy as np
import numpy.typing as npt
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

import hal.config as config
from hal.clock import device_fromtimestamp
from hal.drivers.sensing.crypto import CryptoSession, WSKeyExchangeRequest, resolve_public_key
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import (
    PerceptionStateObservers,
    record_dl_stall,
)
from hal.drivers.sensing.presence_service import PresenceState, PresenseService

from .base import Perception

logger: logging.Logger = logging.getLogger(__name__)


_COCO_SKELETON: list[tuple[int, int]] = [
    (15, 13), (13, 11), (16, 14), (14, 12),
    (11, 12), (5, 11), (6, 12), (5, 6),
    (5, 7), (6, 8), (7, 9), (8, 10),
    (1, 2), (0, 1), (0, 2), (1, 3), (2, 4), (3, 5), (4, 6),
]

_BONE_COLORS: list[tuple[int, int, int]] = [
    (255, 200, 0), (255, 200, 0),
    (0, 100, 255), (0, 100, 255),
    (0, 220, 0),
    (255, 200, 0), (0, 100, 255),
    (0, 220, 0),
    (255, 200, 0), (0, 100, 255),
    (255, 200, 0), (0, 100, 255),
    (0, 220, 0), (0, 220, 0), (0, 220, 0),
    (255, 200, 0), (0, 100, 255),
    (255, 200, 0), (0, 100, 255),
]

_RISK_COLORS: dict[int, tuple[int, int, int]] = {
    1: (0, 200, 0),
    2: (0, 200, 200),
    3: (0, 140, 255),
    4: (0, 0, 255),
}

_CONF_THRESHOLD: float = 0.3


def _draw_pose_2d(
    frame: cv2.typing.MatLike,
    pose_2d: dict[str, Any],
    ergo: dict[str, Any] | None = None,
) -> cv2.typing.MatLike:
    """Draw 2D skeleton and optional ergo score on frame. Returns a copy."""
    vis: npt.NDArray[np.uint8] = frame.copy()
    joints: list[list[float]] = pose_2d.get("joints", [])
    confs: list[float] = pose_2d.get("confs", [])

    if not joints:
        return vis

    kps: npt.NDArray[np.int32] = np.array(joints, dtype=np.int32)

    for idx, (u, v) in enumerate(_COCO_SKELETON):
        if max(u, v) >= len(kps):
            continue
        if confs[u] < _CONF_THRESHOLD or confs[v] < _CONF_THRESHOLD:
            continue
        color: tuple[int, int, int] = _BONE_COLORS[idx] if idx < len(_BONE_COLORS) else (0, 220, 0)
        cv2.line(vis, tuple(kps[u]), tuple(kps[v]), color, 2)

    for i, kp in enumerate(kps):
        if confs[i] < _CONF_THRESHOLD:
            continue
        cv2.circle(vis, tuple(kp), 4, (255, 255, 255), -1)

    if ergo is not None:
        score: int = ergo.get("score", 0)
        risk_level: int = ergo.get("risk_level", 0)
        risk_names: dict[int, str] = {1: "negligible", 2: "low", 3: "medium", 4: "high"}
        label: str = f"RULA: {score} ({risk_names.get(risk_level, '?')})"
        color = _RISK_COLORS.get(risk_level, (200, 200, 200))
        cv2.putText(vis, label, (10, 30), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)

    return vis


@dataclass
class PoseResult:
    pose_2d: dict[str, Any] | None = None
    pose_3d: dict[str, Any] | None = None
    ergo: dict[str, Any] | None = None


@dataclass
class _PoseSample:
    """One posture snapshot recorded into the rolling buffer.

    We do not derive or override anything on this side.
    """

    ts: float
    score: int
    risk_level: int
    raw_left: dict[str, Any] = field(default_factory=dict)
    raw_right: dict[str, Any] = field(default_factory=dict)


class RemotePoseEstimator:
    """WS client to perception-service /api/dl/pose-estimation/ws."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
    ):
        self._base_url: str = base_url
        self._api_key: str = api_key
        self._ws_session: ClientConnection | None = None
        self._crypto: CryptoSession | None = None
        self._last_heartbeat_ts: float = 0.0
        self._heartbeat_interval: float = config.DL_HEARTBEAT_INTERVAL_S

        self._prepare_session()

    def _prepare_session(self) -> None:
        if self._ws_session is not None:
            return

        try:
            ws_url: str = self._base_url.replace("http", "ws").replace("https", "wss")
            logger.info("[%s] connecting to %s", self.__class__.__name__, ws_url)
            self._ws_session = connect(
                ws_url,
                additional_headers={"X-API-Key": self._api_key},
                open_timeout=config.DL_WS_RECV_TIMEOUT_S,
            )
            self._crypto = None
            if config.DL_ENCRYPTION_ENABLED:
                self._setup_crypto()
        except Exception:
            logger.exception("[%s] failed to connect", self.__class__.__name__)
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

    def _img2b64(self, frame: cv2.typing.MatLike) -> str:
        _, buf = cv2.imencode(".jpg", frame)
        return base64.b64encode(buf.tobytes()).decode()

    def _send_heartbeat(self) -> None:
        now: float = time.time()
        if now - self._last_heartbeat_ts < self._heartbeat_interval:
            return
        self._last_heartbeat_ts = now

        if self._ws_session is None:
            return
        try:
            msg = json.dumps({"type": "heartbeat", "task": "pose"})
            if self._crypto is not None:
                msg = self._crypto.wrap_ws_message(msg)
            self._ws_session.send(msg)
            raw = self._ws_session.recv()
            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)
            resp: dict = json.loads(raw)
            if resp.get("status") == "ok":
                logger.debug("[pose] heartbeat ok")
            else:
                logger.warning("[pose] heartbeat unexpected: %s", resp)
        except (ConnectionClosed, TimeoutError):
            logger.warning("[pose] heartbeat failed — connection lost")
            self._ws_session = None

    def update(self, frame: cv2.typing.MatLike) -> PoseResult | None:
        """Send a frame and return the pose result, or None if unavailable."""
        if self._ws_session is None:
            self._prepare_session()
            if self._ws_session is not None:
                logger.info("[%s] reconnected", self.__class__.__name__)

        self._send_heartbeat()

        if self._ws_session is None:
            return None

        try:
            msg = json.dumps(
                {
                    "type": "frame",
                    "task": "pose",
                    "frame_b64": self._img2b64(frame),
                }
            )
            if self._crypto is not None:
                msg = self._crypto.wrap_ws_message(msg)

            self._ws_session.send(msg)
            raw: str = self._ws_session.recv(timeout=config.DL_WS_RECV_TIMEOUT_S)

            if self._crypto is not None:
                raw = self._crypto.unwrap_ws_message(raw)

            resp: dict = json.loads(raw)

            if "error" in resp:
                logger.warning("[pose] backend error: %s", resp["error"])
                return None

            return PoseResult(
                pose_2d=resp.get("pose_2d"),
                pose_3d=resp.get("pose_3d"),
                ergo=resp.get("ergo"),
            )
        except TimeoutError:
            # Backend accepted the frame but never replied within the budget.
            # Drop the session so the worker is freed immediately instead of
            # blocking forever and starving other perceptions.
            logger.warning(
                "[%s] recv timeout (%.1fs) — dropping session, retry next tick",
                self.__class__.__name__,
                config.DL_WS_RECV_TIMEOUT_S,
            )
            record_dl_stall(
                "pose", f"frame recv timeout {config.DL_WS_RECV_TIMEOUT_S:.0f}s"
            )
            self._ws_session = None
            return None
        except ConnectionClosed:
            logger.warning(
                "[%s] connection lost, will retry on next tick", self.__class__.__name__
            )
            self._ws_session = None
            return None

    def ready(self) -> bool:
        return self._ws_session is not None

    def close(self) -> None:
        if self._ws_session is not None:
            try:
                self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None


_REGIONS: tuple[str, ...] = ("neck", "trunk", "upper_arm", "lower_arm", "wrist")


def _dir_size(path: str) -> int:
    total: int = 0
    try:
        with os.scandir(path) as it:
            for de in it:
                if de.is_file(follow_symlinks=False):
                    try:
                        total += de.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
    except OSError:
        pass
    return total

_SIGNED_ANGLE_KEYS: tuple[str, ...] = ("neck_angle", "trunk_angle", "upper_arm_angle")


def _flip_signed_angles(side: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of `side` (a per-side ergo dict from perception-service) with the
    three signed angle keys negated inside `body_scores`.
    """
    if not side:
        return side
    bs: dict[str, Any] | None = side.get("body_scores")
    if not isinstance(bs, dict):
        return side
    new_bs: dict[str, Any] = dict(bs)
    for key in _SIGNED_ANGLE_KEYS:
        val = new_bs.get(key)
        if isinstance(val, (int, float)):
            new_bs[key] = -val
    out: dict[str, Any] = dict(side)
    out["body_scores"] = new_bs
    return out


class PosePerception(Perception[cv2.typing.MatLike]):
    """Pose estimation + silent ergonomic sampling.

    3. NEVER emit an event directly — MotionPerception calls get_posture_summary() and
    decides whether to fold it into the next motion.activity payload.
    """

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None,
        base_url: str = config.DL_POSE_BACKEND_URL,
        api_key: str = config.DL_API_KEY,
    ):
        super().__init__(perception_state, send_event)
        self._presence_service: PresenseService | None = presense_service
        self._estimator: RemotePoseEstimator = RemotePoseEstimator(
            base_url=base_url,
            api_key=api_key,
        )
        self._last_result: PoseResult | None = None
        self._risk_threshold: int = config.POSE_ERGO_HIGH_RISK_THRESHOLD
        # Tumbling window: samples accumulate until POSE_WINDOW_DURATION_S elapses since
        # the first sample, then MotionPerception evaluates and calls reset_window() to
        # start a fresh cycle.
        max_expected: int = int(
            config.POSE_WINDOW_DURATION_S / max(config.POSE_SAMPLE_INTERVAL_S, 1.0)
        )
        self._samples: deque[_PoseSample] = deque(maxlen=max(50, max_expected * 2))
        self._last_sample_ts: float = 0.0
        self._window_start_ts: float = 0.0
        self._samples_dir: str = os.path.join(
            config.SNAPSHOT_TMP_DIR, "sensing_pose"
        )
        self._buckets_dir: str = os.path.join(self._samples_dir, "buckets")
        os.makedirs(self._buckets_dir, exist_ok=True)
        self._bucket_snapshots: list[dict[str, Any]] = []

    def _samples_file_path(self, ts: float) -> str:
        day: str = device_fromtimestamp(ts).strftime("%Y-%m-%d")
        return os.path.join(self._samples_dir, f"samples_{day}.jsonl")

    def _current_bucket_id(self) -> str:
        return f"{int(self._window_start_ts)}" if self._window_start_ts > 0 else ""

    def _current_bucket_dir(self) -> str:
        bid: str = self._current_bucket_id()
        return os.path.join(self._buckets_dir, bid) if bid else ""

    def _snapshot_filename(self, ts: float, score: int) -> str:
        return f"{int(ts)}_{int(score)}.jpg"

    def _save_event_snapshot(
        self,
        frame: cv2.typing.MatLike,
        result: PoseResult,
        sample: "_PoseSample",
    ) -> None:
        """Persist an annotated snapshot into the current bucket so the monitor can click
        any sample row + so /dm can surface the worst frames at end-of-window.
        """
        bucket_dir: str = self._current_bucket_dir()
        if not bucket_dir:
            return
        try:
            os.makedirs(bucket_dir, exist_ok=True)
            annotated: cv2.typing.MatLike = _draw_pose_2d(
                frame, result.pose_2d, result.ergo
            )
            filename: str = self._snapshot_filename(sample.ts, sample.score)
            cv2.imwrite(
                os.path.join(bucket_dir, filename),
                annotated,
                [int(cv2.IMWRITE_JPEG_QUALITY), 80],
            )
            self._bucket_snapshots.append(
                {
                    "ts": round(sample.ts, 2),
                    "score": sample.score,
                    "risk_level": sample.risk_level,
                    "filename": filename,
                    "left": sample.raw_left,
                    "right": sample.raw_right,
                }
            )
        except Exception as e:
            logger.debug("[pose.sample] snapshot save failed: %s", e)

    def _finalize_bucket(self, keep: bool, summary: dict[str, Any] | None) -> None:
        """Close the current bucket."""
        bucket_dir: str = self._current_bucket_dir()
        if not bucket_dir or not os.path.isdir(bucket_dir):
            self._bucket_snapshots = []
            return

        if not keep:
            try:
                shutil.rmtree(bucket_dir)
                logger.debug("[pose.bucket] dropped ephemeral bucket %s", bucket_dir)
            except OSError as e:
                logger.debug("[pose.bucket] drop failed: %s", e)
            self._bucket_snapshots = []
            return

        worst: list[str] = (
            list(summary.get("worst_snapshots", []) or [])
            if summary
            else self._select_worst_snapshots(summary)
        )
        payload: dict[str, Any] = {
            "bucket_id": self._current_bucket_id(),
            "window_start_ts": round(self._window_start_ts, 2),
            "window_end_ts": round(time.time(), 2),
            "kept": True,
            "summary": summary,
            "samples": list(self._bucket_snapshots),
            "worst_snapshots": worst,
        }
        try:
            with open(os.path.join(bucket_dir, "bucket.json"), "w", encoding="utf-8") as fh:
                json.dump(payload, fh, separators=(",", ":"))
            # Touch a sentinel for the pruner: presence of `.kept` flips a
            # bucket into the long-retention pool (POSE_BUCKET_KEEP_S),
            # absence means stale (window never closed properly).
            open(os.path.join(bucket_dir, ".kept"), "a").close()
        except OSError as e:
            logger.warning("[pose.bucket] finalize write failed: %s", e)

        self._bucket_snapshots = []
        self._prune_old_buckets()

    def _select_worst_sample_keys(self, summary: dict[str, Any] | None) -> list[int]:
        """Pick up to POSE_WORST_SNAPSHOTS_PER_BUCKET sample ts keys that cover the cases a
        user would want to see: highest ergo score, the dominant-region representative,
        and the latest bad sample.
        """
        cap: int = max(1, config.POSE_WORST_SNAPSHOTS_PER_BUCKET)
        if not self._bucket_snapshots:
            return []

        sub_thr: int = config.POSE_REGION_HIGH_SUBSCORE
        dominant: str = (summary or {}).get("dominant_region", "") or ""

        by_ts: dict[int, _PoseSample] = {int(s.ts): s for s in self._samples}

        def _hi_in_region(s: _PoseSample, region: str) -> bool:
            for body in (
                (s.raw_left or {}).get("body_scores", {}) or {},
                (s.raw_right or {}).get("body_scores", {}) or {},
            ):
                try:
                    if int(body.get(region, 0) or 0) >= sub_thr:
                        return True
                except (TypeError, ValueError):
                    continue
            return False

        bad_keys: list[int] = []
        for entry in self._bucket_snapshots:
            ts_key: int = int(entry["ts"])
            s: _PoseSample | None = by_ts.get(ts_key)
            if s is None:
                continue
            if s.risk_level >= 2 or any(_hi_in_region(s, r) for r in _REGIONS):
                bad_keys.append(ts_key)
        if not bad_keys:
            bad_keys = [int(e["ts"]) for e in self._bucket_snapshots]

        selected: list[int] = []

        bad_keys_by_score: list[int] = sorted(
            bad_keys,
            key=lambda k: (by_ts[k].score if k in by_ts else 0),
            reverse=True,
        )
        if bad_keys_by_score:
            selected.append(bad_keys_by_score[0])

        if dominant:
            cands: list[int] = [
                k for k in bad_keys_by_score
                if k not in selected and k in by_ts and _hi_in_region(by_ts[k], dominant)
            ]
            if cands:
                selected.append(cands[0])

        for k in sorted(bad_keys, reverse=True):
            if k not in selected:
                selected.append(k)
                break

        for k in bad_keys_by_score:
            if len(selected) >= cap:
                break
            if k not in selected:
                selected.append(k)

        return sorted(set(selected))[:cap]

    def _select_worst_snapshots(self, summary: dict[str, Any] | None) -> list[str]:
        """Filenames of the worst frames — what the DM gallery + the monitor preview attach
        to.
        """
        keys: list[int] = self._select_worst_sample_keys(summary)
        if not keys or not self._bucket_snapshots:
            return []
        ts_to_file: dict[int, str] = {
            int(e["ts"]): e["filename"] for e in self._bucket_snapshots
        }
        return [ts_to_file[k] for k in keys if k in ts_to_file]

    def _worst_sample_aggregate(self, summary: dict[str, Any] | None) -> dict[str, Any]:
        """Max-of-worst stats across the same samples picked for the DM attach."""
        keys: list[int] = self._select_worst_sample_keys(summary)
        if not keys:
            return {}
        by_ts: dict[int, _PoseSample] = {int(s.ts): s for s in self._samples}
        picks: list[_PoseSample] = [by_ts[k] for k in keys if k in by_ts]
        if not picks:
            return {}

        def _side_score(side: dict[str, Any] | None) -> int:
            if not side:
                return 0
            try:
                return int(side.get("score", 0) or 0)
            except (TypeError, ValueError):
                return 0

        return {
            "worst_score": max(s.score for s in picks),
            "worst_risk_level": max(s.risk_level for s in picks),
            "worst_left_score": max(_side_score(s.raw_left) for s in picks),
            "worst_right_score": max(_side_score(s.raw_right) for s in picks),
        }

    def _prune_old_buckets(self) -> None:
        """Drop kept buckets older than POSE_BUCKET_KEEP_S and trim the kept-bucket pool to
        fit POSE_SNAPSHOT_MAX_BYTES (oldest first).

        than one window duration — these are buckets whose finalize never ran (process
        killed mid-window).
        """
        now: float = time.time()
        keep_s: float = config.POSE_BUCKET_KEEP_S
        orphan_grace: float = max(config.POSE_WINDOW_DURATION_S * 2, 600.0)

        try:
            entries: list[tuple[float, int, str, bool]] = []
            with os.scandir(self._buckets_dir) as it:
                for de in it:
                    if not de.is_dir():
                        continue
                    # Skip the currently-open bucket so we never prune our
                    # own working dir mid-window.
                    if self._window_start_ts > 0 and de.name == self._current_bucket_id():
                        continue
                    path: str = de.path
                    kept_marker: str = os.path.join(path, ".kept")
                    is_kept: bool = os.path.exists(kept_marker)
                    try:
                        mtime: float = os.path.getmtime(path)
                    except OSError:
                        continue
                    size: int = _dir_size(path)
                    entries.append((mtime, size, path, is_kept))
        except OSError as e:
            logger.debug("[pose.bucket] prune scan failed: %s", e)
            return

        if not entries:
            return

        entries.sort(key=lambda e: e[0])

        survivors: list[tuple[float, int, str, bool]] = []
        for mtime, size, path, is_kept in entries:
            age: float = now - mtime
            if not is_kept and age > orphan_grace:
                try:
                    shutil.rmtree(path)
                    logger.debug("[pose.bucket] swept orphan %s (age %.0fs)", path, age)
                except OSError:
                    pass
                continue
            if is_kept and age > keep_s:
                try:
                    shutil.rmtree(path)
                except OSError:
                    pass
                continue
            survivors.append((mtime, size, path, is_kept))

        total: int = sum(s for _, s, _, _ in survivors)
        i: int = 0
        max_bytes: int = config.POSE_SNAPSHOT_MAX_BYTES
        while total > max_bytes and i < len(survivors):
            _, size, path, _ = survivors[i]
            try:
                shutil.rmtree(path)
                total -= size
            except OSError:
                pass
            i += 1

    def _append_sample_file(self, sample: _PoseSample) -> None:
        # Pass through perception-service's raw left / right dicts verbatim
        # (body_scores + angles + skipped_joints from Khanh's RULA scorer).
        # We do not derive any new value; aggregation reads these as-is.
        payload: dict[str, Any] = {
            "ts": round(sample.ts, 2),
            "score": sample.score,
            "risk_level": sample.risk_level,
            "left": sample.raw_left,
            "right": sample.raw_right,
        }
        try:
            path: str = self._samples_file_path(sample.ts)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, separators=(",", ":")) + "\n")
        except OSError as e:
            logger.debug("[pose.sample] file append failed: %s", e)

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        if data is None:
            logger.debug("[pose] frame is None, skipping")
            return

        result: PoseResult | None = self._estimator.update(data)
        if result is None:
            logger.debug("[pose] no result from estimator")
            return
        logger.debug("[pose] result: ergo=%s", result.ergo.get("score") if result.ergo else None)

        self._last_result = result

        ergo: dict[str, Any] | None = result.ergo
        if ergo is None:
            return

        now: float = time.time()

        if (
            self._presence_service is not None
            and self._presence_service.state != PresenceState.PRESENT
        ):
            if self._samples or self._window_start_ts > 0:
                logger.debug(
                    "[pose.sample] presence lost — window reset (had %d samples)",
                    len(self._samples),
                )
                self.reset_window()
                self._last_sample_ts = 0.0
            return

        if now - self._last_sample_ts < config.POSE_SAMPLE_INTERVAL_S:
            return

        left: dict[str, Any] = ergo.get("left", {}) or {}
        right: dict[str, Any] = ergo.get("right", {}) or {}
        if config.POSE_FLIP_DLBACKEND_ANGLE_SIGN:
            left = _flip_signed_angles(left)
            right = _flip_signed_angles(right)

        sample = _PoseSample(
            ts=now,
            score=int(ergo.get("score", 0) or 0),
            risk_level=int(ergo.get("risk_level", 0) or 0),
            raw_left=dict(left),
            raw_right=dict(right),
        )
        self._samples.append(sample)
        self._last_sample_ts = now
        self._append_sample_file(sample)
        self._save_event_snapshot(data, result, sample)
        window_age: float = (now - self._window_start_ts) if self._window_start_ts > 0 else 0.0
        logger.debug(
            "[pose.sample] ts=%.0f score=%d risk=%d samples=%d window_age=%.1fs",
            now,
            sample.score,
            sample.risk_level,
            len(self._samples),
            window_age,
        )

    def is_window_complete(self) -> bool:
        """True when the tumbling window has been open for at least POSE_WINDOW_DURATION_S."""
        if self._window_start_ts <= 0.0:
            return False
        return time.time() - self._window_start_ts >= config.POSE_WINDOW_DURATION_S

    def start_window(self) -> None:
        """Open a new tumbling window."""
        if self._window_start_ts > 0.0:
            return
        self._samples.clear()
        self._bucket_snapshots = []
        self._window_start_ts = time.time()
        bucket_dir: str = self._current_bucket_dir()
        if bucket_dir:
            try:
                os.makedirs(bucket_dir, exist_ok=True)
            except OSError as e:
                logger.debug("[pose.bucket] start create failed: %s", e)

    def reset_window(self) -> None:
        """Clear samples + finalize the bucket and unanchor the window. After reset,
        start_window() must be called again before a new cycle.

        reset, start_window() must be called again before a new cycle.
        """
        summary: dict[str, Any] | None = self._aggregate() if self._samples else None
        keep: bool = bool(
            summary is not None
            and summary.get("bad_ratio", 0.0) >= config.POSE_BAD_RATIO
        )
        self._finalize_bucket(keep, summary)
        self._samples.clear()
        self._window_start_ts = 0.0

    def _aggregate(self) -> dict[str, Any] | None:
        """Pure aggregation over whatever samples are currently in the deque."""
        if not self._samples:
            return None

        sub_thr: int = config.POSE_REGION_HIGH_SUBSCORE

        def _hi_subscore(s: _PoseSample) -> bool:
            body_l: dict[str, Any] = (s.raw_left or {}).get("body_scores", {}) or {}
            body_r: dict[str, Any] = (s.raw_right or {}).get("body_scores", {}) or {}
            for region in _REGIONS:
                for body in (body_l, body_r):
                    try:
                        if int(body.get(region, 0) or 0) >= sub_thr:
                            return True
                    except (TypeError, ValueError):
                        continue
            return False

        bad: list[_PoseSample] = [
            s for s in self._samples
            if s.risk_level >= 2 or _hi_subscore(s)
        ]
        bad_ratio: float = len(bad) / len(self._samples)

        region_freq: dict[str, int] = {region: 0 for region in _REGIONS}
        for s in bad:
            body_l: dict[str, Any] = (s.raw_left or {}).get("body_scores", {}) or {}
            body_r: dict[str, Any] = (s.raw_right or {}).get("body_scores", {}) or {}
            for region in _REGIONS:
                try:
                    l = int(body_l.get(region, 0))
                except (TypeError, ValueError):
                    l = 0
                try:
                    r = int(body_r.get(region, 0))
                except (TypeError, ValueError):
                    r = 0
                if l >= sub_thr or r >= sub_thr:
                    region_freq[region] += 1

        dominant_region: str = ""
        dominant_count: int = 0
        if bad:
            dominant_region = max(region_freq, key=lambda r: region_freq[r])
            dominant_count = region_freq[dominant_region]

        latest: _PoseSample = self._samples[-1]
        window_min: int = int(config.POSE_WINDOW_DURATION_S / 60)
        summary: dict[str, Any] = {
            "bad_ratio": round(bad_ratio, 2),
            "samples": len(self._samples),
            "bad_samples": len(bad),
            "window_min": window_min,
            "region_frequency": region_freq,
            "dominant_region": dominant_region,
            "dominant_count": dominant_count,
            "latest_score": latest.score,
            "latest_risk_level": latest.risk_level,
            "latest_left": latest.raw_left,
            "latest_right": latest.raw_right,
            "bucket_id": self._current_bucket_id(),
        }
        summary["worst_snapshots"] = self._select_worst_snapshots(summary)
        summary.update(self._worst_sample_aggregate(summary))
        return summary

    def get_posture_summary(self) -> dict[str, Any] | None:
        """Gated aggregation: returns the summary only when the window has elapsed AND has
        at least POSE_WINDOW_MIN_SAMPLES samples (statistical noise floor — detection
        misses can leave a window too sparse to trust).
        """
        if not self.is_window_complete():
            return None
        if len(self._samples) < config.POSE_WINDOW_MIN_SAMPLES:
            return None
        return self._aggregate()

    def is_window_bad(self) -> bool:
        """True when the gate criteria are met (window complete AND bad ratio over threshold)."""
        summary: dict[str, Any] | None = self.get_posture_summary()
        if summary is None:
            return False
        return summary["bad_ratio"] >= config.POSE_BAD_RATIO

    def draw_latest_overlay(
        self, frame: cv2.typing.MatLike
    ) -> cv2.typing.MatLike:
        """Annotate a frame with the most recent pose result (for snapshots)."""
        if self._last_result is None or self._last_result.pose_2d is None:
            return frame
        return _draw_pose_2d(
            frame, self._last_result.pose_2d, self._last_result.ergo
        )

    @override
    def cleanup(self) -> None:
        self._estimator.close()

    def to_dict(self) -> dict[str, Any]:
        ergo_score: int | None = None
        ergo_risk: int | None = None
        has_pose_2d: bool = False
        has_pose_3d: bool = False

        if self._last_result is not None:
            has_pose_2d = self._last_result.pose_2d is not None
            has_pose_3d = self._last_result.pose_3d is not None
            if self._last_result.ergo is not None:
                ergo_score = self._last_result.ergo.get("score")
                ergo_risk = self._last_result.ergo.get("risk_level")

        seconds_since_sample: float | None = None
        if self._last_sample_ts > 0:
            seconds_since_sample = time.time() - self._last_sample_ts
        window_age_s: float = 0.0
        if self._window_start_ts > 0:
            window_age_s = time.time() - self._window_start_ts

        return {
            "type": "pose",
            "connected": self._estimator.ready(),
            "has_pose_2d": has_pose_2d,
            "has_pose_3d": has_pose_3d,
            "ergo_score": ergo_score,
            "ergo_risk_level": ergo_risk,
            "seconds_since_sample": int(seconds_since_sample)
            if seconds_since_sample is not None
            else None,
            "samples_in_buffer": len(self._samples),
            "window_age_s": int(window_age_s),
            "window_duration_s": int(config.POSE_WINDOW_DURATION_S),
            "window_min_samples": config.POSE_WINDOW_MIN_SAMPLES,
            "window_complete": self.is_window_complete(),
            "sample_interval_s": config.POSE_SAMPLE_INTERVAL_S,
            "bad_ratio_threshold": config.POSE_BAD_RATIO,
            "summary": self.get_posture_summary(),
            "running": self._aggregate(),
            "samples": [
                {
                    "ts": round(s.ts, 2),
                    "score": s.score,
                    "risk_level": s.risk_level,
                    "left": s.raw_left,
                    "right": s.raw_right,
                }
                for s in self._samples
            ],
        }
