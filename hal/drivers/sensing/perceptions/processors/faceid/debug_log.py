"""Per-detection debug capture for the v2 face pipeline."""

import json
import logging
import os
import shutil
import threading
import time
from typing import Any

import cv2
import numpy as np

logger = logging.getLogger(__name__)

_LANDMARK_VIEW_PAD = 0.25
_LANDMARK_VIEW_MIN_PX = 320


class FaceIdDebugLogger:
    """Persists every face-recognition decision to its OWN timestamped folder
    under ``config.FACEID_LOG_DIR``.

    All writes are best-effort and lock-guarded — a logging failure never breaks
    detection.
    """

    def __init__(self, root_dir: str, enabled: bool = True, max_triggers: int = 0):
        self._root: str = root_dir
        self._enabled: bool = enabled
        self._max_triggers: int = max_triggers
        self._lock: threading.Lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def _slug(text: Any) -> str:
        """Filesystem-safe token for a folder name."""
        s = "".join(c if c.isalnum() else "-" for c in str(text)).strip("-")
        return s or "unknown"

    @staticmethod
    def _json_default(o: Any) -> Any:
        """Coerce non-JSON types (numpy scalars/arrays) to native values so
        similarities serialize as numbers, not stringified reprs."""
        if hasattr(o, "item"):
            return o.item()
        if hasattr(o, "tolist"):
            return o.tolist()
        return str(o)

    def _annotate(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        label: str,
        similarity: float,
        color: tuple[int, int, int],
    ) -> cv2.typing.MatLike | None:
        try:
            vis = frame.copy()
            x1, y1, x2, y2 = bbox
            _ = cv2.rectangle(vis, (x1, y1), (x2, y2), color, 2)
            _ = cv2.putText(
                vis,
                f"{label} {similarity:.2f}",
                (x1, max(0, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                color,
                2,
            )
            return vis
        except Exception:
            return None

    def _draw_landmarks(
        self,
        frame: cv2.typing.MatLike,
        bbox: list[int],
        landmarks: Any,
        kps5: Any = None,
    ) -> cv2.typing.MatLike | None:
        """Padded face view with the dense mesh plotted over it."""
        if landmarks is None:
            return None
        try:
            h, w = frame.shape[:2]
            x1, y1, x2, y2 = bbox
            bw, bh = x2 - x1, y2 - y1
            if bw <= 0 or bh <= 0:
                return None
            px, py = int(bw * _LANDMARK_VIEW_PAD), int(bh * _LANDMARK_VIEW_PAD)
            vx1, vy1 = max(0, x1 - px), max(0, y1 - py)
            vx2, vy2 = min(w, x2 + px), min(h, y2 + py)
            if vx2 <= vx1 or vy2 <= vy1:
                return None
            view = frame[vy1:vy2, vx1:vx2].copy()
            scale = max(1.0, _LANDMARK_VIEW_MIN_PX / max(view.shape[0], view.shape[1]))
            if scale > 1.0:
                view = cv2.resize(
                    view, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR
                )

            def _pt(p: Any) -> tuple[int, int]:
                return (
                    int(round((float(p[0]) - vx1) * scale)),
                    int(round((float(p[1]) - vy1) * scale)),
                )

            _ = cv2.rectangle(view, _pt((x1, y1)), _pt((x2, y2)), (0, 255, 0), 1)
            for p in np.asarray(landmarks, dtype=np.float32):
                _ = cv2.circle(view, _pt(p), 1, (255, 0, 255), -1)
            if kps5 is not None:
                for p in np.asarray(kps5, dtype=np.float32):
                    _ = cv2.circle(view, _pt(p), 3, (0, 255, 255), -1)
            return view
        except Exception:
            return None

    @staticmethod
    def _landmark_summary(landmarks: Any, kps5: Any) -> dict[str, Any]:
        """The small landmark facts that belong in result.json: how many dense points there
        were, and the 5 warp points themselves (they are what a bad alignment shows up
        in, and 5 pairs stay readable inline).
        """
        out: dict[str, Any] = {
            "landmark_count": 0 if landmarks is None else int(len(landmarks)),
        }
        if kps5 is not None:
            out["kps5"] = np.asarray(kps5, dtype=np.float64).round(2).tolist()
        return out

    @staticmethod
    def _landmark_sidecar(landmarks: Any, kps5: Any) -> dict[str, Any] | None:
        """The dense mesh as its own compact landmarks.json, or None."""
        if landmarks is None:
            return None
        payload: dict[str, Any] = {
            "landmarks": np.asarray(landmarks, dtype=np.float64).round(2).tolist(),
        }
        if kps5 is not None:
            payload["kps5"] = np.asarray(kps5, dtype=np.float64).round(2).tolist()
        return {"landmarks.json": payload}

    def _write_folder(
        self,
        name: str,
        record: dict[str, Any],
        images: dict[str, cv2.typing.MatLike | None],
        sidecars: dict[str, Any] | None = None,
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
                for filename, payload in (sidecars or {}).items():
                    if payload is None:
                        continue
                    with open(
                        os.path.join(folder, filename), "w", encoding="utf-8"
                    ) as f:
                        _ = f.write(
                            json.dumps(
                                payload,
                                default=self._json_default,
                                separators=(",", ":"),
                            )
                        )
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
            logger.debug("[face] debug save failed: %s", e)
            return None

    def _prune(self) -> None:
        """Drop oldest detection folders past the cap. Caller holds the lock."""
        if self._max_triggers <= 0:
            return
        try:
            entries = [
                e
                for e in os.listdir(self._root)
                if os.path.isdir(os.path.join(self._root, e))
            ]
            entries.sort()
            for stale in entries[: max(0, len(entries) - self._max_triggers)]:
                shutil.rmtree(os.path.join(self._root, stale), ignore_errors=True)
        except Exception as e:
            logger.debug("[face] debug prune failed: %s", e)

    def save_decision(
        self,
        face_id: str,
        similarity: float,
        face_crop: cv2.typing.MatLike | None = None,
        aligned: cv2.typing.MatLike | None = None,
        frame: cv2.typing.MatLike | None = None,
        bbox: list[int] | None = None,
        color: tuple[int, int, int] = (0, 255, 0),
        landmarks: Any = None,
        kps5: Any = None,
        **meta: Any,
    ) -> str | None:
        """One face the recognizer reached an identity decision on → its own
        folder, named ``<timestamp>_<face_id>_<similarity>``."""
        if not self._enabled:
            return None
        annotated = (
            self._annotate(frame, bbox, face_id, similarity, color)
            if frame is not None and bbox is not None
            else None
        )
        mesh = (
            self._draw_landmarks(frame, bbox, landmarks, kps5)
            if frame is not None and bbox is not None
            else None
        )
        name = f"{self._slug(face_id)}_{similarity:.2f}"
        record: dict[str, Any] = {
            "status": "decision",
            "face_id": face_id,
            "similarity": similarity,
        }
        if bbox is not None:
            record["bbox"] = list(bbox)
        record.update(meta)
        record.update(self._landmark_summary(landmarks, kps5))
        return self._write_folder(
            name,
            record,
            {
                "input.jpg": face_crop,
                "aligned.jpg": aligned,
                "frame.jpg": frame,
                "annotated.jpg": annotated,
                "face_with_landmark.jpg": mesh,
            },
            self._landmark_sidecar(landmarks, kps5),
        )

    def save_failure(
        self,
        reason: str,
        face_crop: cv2.typing.MatLike | None = None,
        aligned: cv2.typing.MatLike | None = None,
        frame: cv2.typing.MatLike | None = None,
        bbox: list[int] | None = None,
        landmarks: Any = None,
        kps5: Any = None,
        **meta: Any,
    ) -> str | None:
        """One face that never reached an identity decision → its own folder,
        with the input image (when available) and the reason it was dropped."""
        if not self._enabled:
            return None
        mesh = (
            self._draw_landmarks(frame, bbox, landmarks, kps5)
            if frame is not None and bbox is not None
            else None
        )
        name = "FAIL-" + self._slug(reason)
        record: dict[str, Any] = {"status": "failure", "reason": reason}
        if bbox is not None:
            record["bbox"] = list(bbox)
        record.update(meta)
        record.update(self._landmark_summary(landmarks, kps5))
        return self._write_folder(
            name,
            record,
            {
                "input.jpg": face_crop,
                "aligned.jpg": aligned,
                "frame.jpg": frame,
                "face_with_landmark.jpg": mesh,
            },
            self._landmark_sidecar(landmarks, kps5),
        )
