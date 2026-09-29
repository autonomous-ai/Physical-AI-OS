"""EdgeFace recognition pipeline."""

import numpy as np
import onnxruntime as ort

from .edgeface import _EdgeFaceEmbedder
from .landmark import _MediaPipeLandmarkONNX, _OnnxLandmarkAligner
from .model_store import _LANDMARK_CONF_THRESHOLD
from .scrfd import _SCRFDDetector


class _EdgeFacePipeline:
    """SCRFD (detect) -> ONNX landmark (align) -> EdgeFace (embed)."""

    def __init__(
        self,
        scrfd_model_path: str,
        edgeface_model_path: str,
        landmark_model_path: str,
        det_size=640,
        conf_thresh: float = 0.35,
        nms_thresh: float = 0.4,
        det_fp16: bool = False,
        emb_fp16: bool = False,
        landmark_fp16: bool = False,
        landmark_conf_thresh: float = _LANDMARK_CONF_THRESHOLD,
        roi_scale: float = 1.4,
        roll_align: bool = True,
        l2_normalize: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        self.detector = _SCRFDDetector(
            scrfd_model_path,
            input_size=det_size,
            confidence_threshold=conf_thresh,
            nms_threshold=nms_thresh,
            fp16=det_fp16,
            session_options=session_options,
        )
        self.landmarker = _MediaPipeLandmarkONNX(
            landmark_model_path,
            conf_thresh=landmark_conf_thresh,
            roi_scale=roi_scale,
            roll_align=roll_align,
            fp16=landmark_fp16,
            session_options=session_options,
        )
        self.aligner = _OnnxLandmarkAligner(self.landmarker)
        self.embedder = _EdgeFaceEmbedder(
            edgeface_model_path,
            fp16=emb_fp16,
            l2_normalize=l2_normalize,
            session_options=session_options,
        )

    def get(self, frame: np.ndarray) -> list:
        detections = self.detector.infer(frame)
        results = []
        for det in detections:
            bbox = det["bbox"]
            kps = det["kps"]

            aligned, kps, emotion_box, landmarks, lm_score = (
                self.aligner.align_crop_from_bbox(frame, bbox, kps=kps)
            )
            if aligned is None:
                continue

            embedding = self.embedder.get_embedding(aligned)
            results.append(
                {
                    "bbox": bbox.astype(np.float32),
                    "kps": None if kps is None else kps.astype(np.float32),
                    "det_score": np.float32(det["det_score"]),
                    "embedding": embedding.astype(np.float32),
                    "emotion_box": emotion_box,
                    "aligned": aligned,
                    "landmarks": landmarks,
                    "landmark_score": lm_score,
                }
            )
        return results

    __call__ = get
