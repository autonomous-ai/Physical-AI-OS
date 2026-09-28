"""predict_face(gate=False): the ungated argmax plus every class probability.

HAL now owns the per-label gate, so the server must be able to hand back what
the model actually said. The gated path stays the default for every other
caller.
"""

import asyncio
import types

import numpy as np

from core.models.facial_emotion import Emotion, RawEmotionDetection
from core.perception.facial_emotion.perception import EmotionPerception
from dlserver.models.facial_emotion import EmotionRecognizeRequest
from dlserver.routes import facial_emotion as route

CLASSES = ["Neutral", "Happy", "Sad", "Surprise", "Fear", "Disgust", "Anger"]


def _probs(**by_label: float) -> np.ndarray:
    p = np.zeros(len(CLASSES), dtype=np.float32)
    for name, value in by_label.items():
        p[CLASSES.index(name)] = value
    return p


class _Batcher:
    def __init__(self, probs: np.ndarray) -> None:
        self.predictor = types.SimpleNamespace(class_names=CLASSES)
        self._probs = probs

    async def submit(self, crops):
        fut = asyncio.get_running_loop().create_future()
        fut.set_result(RawEmotionDetection(expression_probs=self._probs))
        return [fut]


def _perception(probs: np.ndarray) -> EmotionPerception:
    p = object.__new__(EmotionPerception)
    p._emotion_batcher = _Batcher(probs)
    p._default_config = None
    return p


CROP = np.zeros((40, 30, 3), dtype=np.uint8)


def test_ungated_returns_the_argmax_even_below_its_bar():
    p = _perception(_probs(Sad=0.75, Neutral=0.15, Happy=0.10))
    e = asyncio.run(p.predict_face(CROP, gate=False))
    assert e.emotion == "Sad"
    assert abs(e.confidence - 0.75) < 1e-6


def test_gated_default_is_unchanged():
    p = _perception(_probs(Sad=0.75, Neutral=0.15, Happy=0.10))
    e = asyncio.run(p.predict_face(CROP))
    assert e.emotion == "Neutral"
    assert abs(e.confidence - 0.15) < 1e-6


def test_both_modes_carry_every_class_probability():
    p = _perception(_probs(Sad=0.75, Neutral=0.15, Happy=0.10))
    for gate in (True, False):
        e = asyncio.run(p.predict_face(CROP, gate=gate))
        assert set(e.probabilities) == set(CLASSES)
        assert abs(e.probabilities["Sad"] - 0.75) < 1e-6
        assert all(isinstance(v, float) for v in e.probabilities.values())


class _Model:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def is_ready(self) -> bool:
        return True

    async def predict_face(self, face_crop, **kwargs):
        self.calls.append(kwargs)
        gated = kwargs.get("gate", True)
        return Emotion(
            emotion="Neutral" if gated else "Sad",
            confidence=0.15 if gated else 0.45,
            face_confidence=1.0,
            bbox=[0, 0, 30, 40],
            probabilities={"Neutral": 0.15, "Sad": 0.45, "Happy": 0.40},
        )


def _call(monkeypatch, **req):
    model = _Model()
    monkeypatch.setattr(route, "get_emotion_model", lambda: model)
    monkeypatch.setattr(route, "decode_image", lambda _b64: CROP)
    resp = asyncio.run(route.emotion_recognize(EmotionRecognizeRequest(image_b64="x", **req)))
    return resp, model


def test_raw_bypasses_the_threshold_drop_and_returns_probabilities(monkeypatch):
    resp, model = _call(monkeypatch, threshold=0.5, raw=True)
    assert model.calls == [{"gate": False}]
    [det] = resp.detections
    assert det.emotion == "Sad"
    assert det.probabilities == {"Neutral": 0.15, "Sad": 0.45, "Happy": 0.40}


def test_default_request_is_unchanged_for_old_clients(monkeypatch):
    resp, model = _call(monkeypatch, threshold=0.5)
    assert model.calls == [{}]
    assert resp.detections == []          # Neutral 0.15 < 0.5 → dropped, as today


def test_default_request_never_exposes_probabilities(monkeypatch):
    resp, _ = _call(monkeypatch, threshold=0.0)
    [det] = resp.detections
    assert det.probabilities is None
