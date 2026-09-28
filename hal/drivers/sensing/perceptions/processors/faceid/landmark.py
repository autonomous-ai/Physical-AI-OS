"""MediaPipe FaceMesh landmark regressor (ONNX) + landmark-based aligner."""

import logging
import os

import cv2
import numpy as np
import onnxruntime as ort

from .geometry import (
    _box_from_landmarks,
    _landmarks_out_of_bounds,
    _warp_and_crop_face,
)

logger = logging.getLogger(__name__)

_LM_NOSE = 1
_LM_MOUTH_RIGHT = 287
_LM_MOUTH_LEFT = 57
_LM_RIGHT_EYE = (362, 263)
_LM_LEFT_EYE = (33, 243)


class _MediaPipeLandmarkONNX:
    """ONNX MediaPipe FaceMesh landmark regressor (replaces pip mediapipe)."""

    def __init__(
        self,
        model_path: str,
        input_size=192,
        conf_thresh: float = 0.6,
        roi_scale: float = 1.4,
        roll_align: bool = True,
        fp16: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        """model_path: path to the MediaPipe landmark ONNX model."""
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.conf_thresh = conf_thresh
        self.roi_scale = roi_scale
        self.roll_align = roll_align
        self.fp16 = fp16

    def _blob(self, bgr_crop: np.ndarray) -> np.ndarray:
        """BGR uint8 crop -> (1,3,H,W) RGB blob in [0,1] (MediaPipe order)."""
        img = cv2.resize(bgr_crop, self.input_size)
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        blob = np.transpose(img, (2, 0, 1))[None]
        return blob.astype(np.float16 if self.fp16 else np.float32)

    def _run(self, bgr_crop: np.ndarray):
        """Run the session on a crop. Returns (xy_in_input_px, score)."""
        scores, landmarks = self.session.run(
            self.output_names, {self.input_name: self._blob(bgr_crop)}
        )
        score = float(scores[0])
        xy = landmarks[0][:, :2].astype(np.float32) * np.float32(self.input_size)
        return xy, score

    def _roi_transform(self, bbox, kps=None):
        """Affine (2x3) mapping the frame into a square input_size ROI centered
        on the bbox, optionally roll-corrected using the eye keypoints."""
        x1, y1, x2, y2 = [float(v) for v in bbox[:4]]
        cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
        side = max(x2 - x1, y2 - y1) * self.roi_scale

        angle = 0.0
        if self.roll_align and kps is not None:
            reye, leye = np.asarray(kps)[0], np.asarray(kps)[1]
            angle = float(
                np.degrees(np.arctan2(leye[1] - reye[1], leye[0] - reye[0]))
            )

        out = self.input_size[0]
        M = cv2.getRotationMatrix2D((cx, cy), angle, out / side)
        M[0, 2] += out / 2.0 - cx
        M[1, 2] += out / 2.0 - cy
        return M

    def detect_in_frame(self, frame: np.ndarray, bbox, kps=None):
        """Detect 468 landmarks for one face, in FULL-FRAME pixel coords."""
        M = self._roi_transform(bbox, kps)
        roi = cv2.warpAffine(frame, M, self.input_size)
        xy, score = self._run(roi)
        Minv = cv2.invertAffineTransform(M)
        pts = np.hstack([xy, np.ones((xy.shape[0], 1), np.float32)])
        xy_frame = (pts @ Minv.T).astype(np.float32)
        return xy_frame, score

    @staticmethod
    def to_5points(landmarks: np.ndarray) -> np.ndarray:
        """Reduce the 468 dense landmarks to the 5 canonical points in the order
        the ArcFace template expects: [reye, leye, nose, mouthright, mouthleft]."""
        lm = np.asarray(landmarks, dtype=np.float32)
        reye = lm[list(_LM_RIGHT_EYE)].mean(axis=0)
        leye = lm[list(_LM_LEFT_EYE)].mean(axis=0)
        return np.stack(
            [reye, leye, lm[_LM_NOSE], lm[_LM_MOUTH_RIGHT], lm[_LM_MOUTH_LEFT]],
            axis=0,
        ).astype(np.float32)


class _OnnxLandmarkAligner:
    """ONNX-landmark alignment (drop-in for the old MediaPipe FaceMesh aligner).

    A value like 0.6 does not mean "fairly strict" here — it means the gate never fires.
    """

    def __init__(self, landmarker: _MediaPipeLandmarkONNX) -> None:
        self._landmarker = landmarker

    def align_crop_from_bbox(self, frame: np.ndarray, bbox, kps=None):
        """Aligned 112x112 crop for one detection.

        Faces whose landmark confidence is below ``conf_thresh`` (or whose landmarks
        fall outside the bbox/image) are dropped.
        """
        try:
            landmarks, score = self._landmarker.detect_in_frame(frame, bbox, kps=kps)
        except Exception as e:  # noqa: BLE001
            logger.debug("[face-v2] landmark inference error: %s", e)
            return None, None, None, None, 0.0

        if landmarks is not None and score >= self._landmarker.conf_thresh:
            pts5 = self._landmarker.to_5points(landmarks)
            if not _landmarks_out_of_bounds(pts5, bbox, frame.shape):
                try:
                    aligned = _warp_and_crop_face(frame, pts5)
                    h, w = frame.shape[:2]
                    emotion_box = _box_from_landmarks(landmarks, w, h)
                    return aligned, pts5, emotion_box, landmarks, score
                except Exception as e:  # noqa: BLE001
                    logger.debug("[face-v2] landmark alignment error: %s", e)

        return None, None, None, landmarks, score
