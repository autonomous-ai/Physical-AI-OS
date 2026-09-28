"""EdgeFace face embedder (ONNX) — aligned 112x112 crop -> embedding vector."""

import os

import cv2
import numpy as np
import onnxruntime as ort


class _EdgeFaceEmbedder:
    def __init__(
        self,
        model_path: str,
        input_size=112,
        fp16: bool = False,
        l2_normalize: bool = False,
        session_options: ort.SessionOptions | None = None,
    ):
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.fp16 = fp16
        self.l2_normalize = l2_normalize
        self.mean = 0.5
        self.std = 0.5

    def preprocess(self, aligned_face: np.ndarray) -> np.ndarray:
        img = aligned_face
        if img.shape[1] != self.input_size[0] or img.shape[0] != self.input_size[1]:
            img = cv2.resize(img, self.input_size)
        img = img.astype(np.float32) / 255.0
        img = (img - self.mean) / self.std
        blob = np.transpose(img, (2, 0, 1))[None]
        return blob.astype(np.float16 if self.fp16 else np.float32)

    def _postprocess(self, embeddings: np.ndarray) -> np.ndarray:
        embeddings = embeddings.astype(np.float32)
        if self.l2_normalize:
            norms = np.linalg.norm(embeddings, axis=1, keepdims=True)
            embeddings = embeddings / (norms + 1e-10)
        return embeddings

    def get_embedding(self, aligned_face: np.ndarray) -> np.ndarray:
        blob = self.preprocess(aligned_face)
        out = self.session.run(self.output_names, {self.input_name: blob})[0]
        return self._postprocess(out)[0]
