"""RemoteEmotionRecognizer against new and old perception servers.

HAL always asks for raw probabilities. A new server returns them and HAL gates;
an old server ignores `raw`, gates itself and returns no probabilities, and HAL
must then take its label as-is. Either way a failed gate is None — the caller
records that as no reading.
"""

import json
import threading
import types

from hal.drivers.sensing.perceptions.processors import emotion as mod
from hal.drivers.sensing.perceptions.processors.emotion import (
    EmotionPerception,
    RemoteEmotionRecognizer,
)
from hal.drivers.sensing.perceptions.processors.emotion_gating import (
    DEFAULT_LABEL_THRESHOLDS,
)

import numpy as np

CROP = np.zeros((40, 30, 3), dtype=np.uint8)


class _StubDebug:
    """Records every save_failure/save_prediction call's kwargs, for pinning
    which fields make it into result.json without touching the filesystem."""

    def __init__(self):
        self.failures: list[tuple[str, dict]] = []
        self.predictions: list[dict] = []

    def save_failure(self, reason, **meta):
        self.failures.append((reason, meta))
        return None

    def save_prediction(self, **meta):
        self.predictions.append(meta)
        return None


def _recognizer(debug=None):
    r = object.__new__(RemoteEmotionRecognizer)
    r._url = "http://dl/hal/api/dl/emotion-recognize"
    r._api_key = "k"
    r._threshold = 0.5
    r._timeout = 1.0
    r._crypto = None
    r._debug = debug
    r._backoff_until = 0.0
    r._label_thresholds = dict(DEFAULT_LABEL_THRESHOLDS)
    return r


def _serve(monkeypatch, detections):
    sent = []

    def post(url, data, headers, timeout):
        sent.append(json.loads(data))
        return types.SimpleNamespace(
            status_code=200, text="", json=lambda: {"detections": detections}
        )

    monkeypatch.setattr(mod.requests, "post", post)
    return sent


def _det(emotion, confidence, probabilities=None):
    d = {"emotion": emotion, "confidence": confidence, "face_confidence": 1.0,
         "bbox": [0, 0, 30, 40], "valence": None, "arousal": None}
    if probabilities is not None:
        d["probabilities"] = probabilities
    return d


def test_hal_asks_for_raw(monkeypatch):
    sent = _serve(monkeypatch, [])
    _recognizer().recognize(CROP)
    assert sent[0]["raw"] is True


def test_new_server_sad_below_hal_bar_is_no_reading(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.75, {"Neutral": 0.15, "Sad": 0.75, "Happy": 0.10})])
    assert _recognizer().recognize(CROP) is None


def test_new_server_sad_above_hal_bar_stands(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.85, {"Neutral": 0.10, "Sad": 0.85, "Happy": 0.05})])
    result = _recognizer().recognize(CROP)
    assert (result["emotion"], result["gate"]) == ("Sad", "hal")
    assert result["probabilities"]["Sad"] == 0.85


def test_hal_thresholds_are_what_decide(monkeypatch):
    _serve(monkeypatch, [_det("Sad", 0.75, {"Neutral": 0.15, "Sad": 0.75, "Happy": 0.10})])
    r = _recognizer()
    r._label_thresholds = {"sad": 0.7}
    assert r.recognize(CROP)["emotion"] == "Sad"


def test_old_server_label_is_trusted(monkeypatch):
    # Old server: ignored `raw`, gated with its own map, sent no probabilities.
    _serve(monkeypatch, [_det("Sad", 0.75)])
    result = _recognizer().recognize(CROP)
    assert (result["emotion"], result["gate"]) == ("Sad", "server")


def test_null_probabilities_is_treated_as_old_server(monkeypatch):
    d = _det("Happy", 0.6)
    d["probabilities"] = None
    _serve(monkeypatch, [d])
    assert _recognizer().recognize(CROP)["gate"] == "server"


def test_failed_hal_gate_is_saved_with_gate_hal(monkeypatch):
    # Pins the fix: a reading that fails the HAL bar is recorded with
    # gate="hal" in the debug failure folder, not silently omitted.
    _serve(monkeypatch, [_det("Sad", 0.75, {"Neutral": 0.15, "Sad": 0.75, "Happy": 0.10})])
    debug = _StubDebug()
    r = _recognizer(debug=debug)
    assert r.recognize(CROP) is None
    assert len(debug.failures) == 1
    reason, meta = debug.failures[0]
    assert reason == "gated"
    assert meta["gate"] == "hal"


def test_process_face_records_gate_in_debug_prediction(monkeypatch):
    # Pins the fix: a successful reading's gate ("hal" or "server") reaches
    # save_prediction, which is what result.json is built from — not just
    # the return value of recognize().
    _serve(monkeypatch, [_det("Sad", 0.85, {"Neutral": 0.10, "Sad": 0.85, "Happy": 0.05})])
    debug = _StubDebug()

    # Minimal EmotionPerception carrying only what _process_face reads,
    # following the object.__new__ pattern in test_emotion_occupancy.py —
    # no network, no config, no real debug logger.
    p = object.__new__(EmotionPerception)
    p._recognizer = _recognizer(debug=debug)
    p._debug = debug
    p._record_attempt = lambda person_id, label: None
    p._presence_service = None
    p._state_lock = threading.RLock()
    p._last_detection_time = None
    p._last_emotion = None
    p._emotion_buffer = {}

    frame = np.zeros((40, 30, 3), dtype=np.uint8)
    face = types.SimpleNamespace(
        emotion_box=None, bbox=[0, 0, 30, 40], person_id="alice", confidence=0.9
    )
    p._process_face(frame, face)

    assert len(debug.predictions) == 1
    assert debug.predictions[0]["gate"] == "hal"
