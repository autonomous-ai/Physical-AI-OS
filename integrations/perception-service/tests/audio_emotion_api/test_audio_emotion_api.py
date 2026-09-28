"""HTTP integration tests for /ser/recognize.

Requires DL_BACKEND_URL, DL_API_KEY and tests/fixtures/audio/; skipped if the model is not loaded.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import pytest
from dotenv import load_dotenv

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "").rstrip("/")
DL_API_KEY = os.getenv("DL_API_KEY", "")

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"
HAPPY_WAV = FIXTURES_DIR / "happy.wav"
SAD_WAV = FIXTURES_DIR / "sad.wav"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL,
    reason="DL_BACKEND_URL not set - skipping audio emotion API tests.",
)


def _headers() -> dict[str, str]:
    h: dict[str, str] = {"Content-Type": "application/json"}
    if DL_API_KEY:
        h["X-API-Key"] = DL_API_KEY
    return h


def _url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _wav_to_b64(path: Path) -> str:
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _post_recognize(audio_b64: str, return_scores: bool = True) -> httpx.Response:
    payload: dict = {"audio_b64": audio_b64, "return_scores": return_scores}
    return httpx.post(
        _url("/hal/api/dl/ser/recognize"),
        json=payload,
        headers=_headers(),
        timeout=30.0,
    )


def _api_available() -> bool:
    if not HAPPY_WAV.exists():
        return False
    try:
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV))
        return resp.status_code == 200
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def check_api():
    if not FIXTURES_DIR.exists():
        pytest.skip("Audio fixtures not found")
    if not _api_available():
        pytest.skip("Audio emotion endpoint not available")


class TestBasicResponse:
    def test_recognize_returns_200(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV))
        assert resp.status_code == 200

    def test_response_has_label_and_confidence(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV))
        data = resp.json()
        assert "label" in data
        assert "confidence" in data
        assert isinstance(data["label"], str)
        assert isinstance(data["confidence"], float)

    def test_scores_sum_to_one(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV), return_scores=True)
        data = resp.json()
        total = sum(data["scores"].values())
        assert abs(total - 1.0) < 1e-3

    def test_return_scores_true(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV), return_scores=True)
        data = resp.json()
        assert data["scores"] is not None
        assert isinstance(data["scores"], dict)
        assert len(data["scores"]) > 0

    def test_return_scores_false(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV), return_scores=False)
        data = resp.json()
        assert data.get("scores") is None

    def test_scores_sorted_by_confidence(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV), return_scores=True)
        data = resp.json()
        values = list(data["scores"].values())
        assert values == sorted(values, reverse=True)


class TestEmotionDetection:
    def test_happy_detected(self):
        resp = _post_recognize(_wav_to_b64(HAPPY_WAV))
        data = resp.json()
        assert data["label"] == "happy"
        assert data["confidence"] > 0.5

    def test_sad_detected(self):
        resp = _post_recognize(_wav_to_b64(SAD_WAV))
        data = resp.json()
        assert data["label"] == "sad"
        assert data["confidence"] > 0.5

    def test_batch_both_detected(self):
        happy_resp = _post_recognize(_wav_to_b64(HAPPY_WAV))
        sad_resp = _post_recognize(_wav_to_b64(SAD_WAV))
        assert happy_resp.json()["label"] == "happy"
        assert sad_resp.json()["label"] == "sad"


class TestLabelsEndpoint:
    def test_get_labels(self):
        resp = httpx.get(
            _url("/hal/api/dl/ser/labels"),
            headers=_headers(),
            timeout=10.0,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "engine" in data
        assert "labels" in data
        assert isinstance(data["labels"], list)
        assert len(data["labels"]) > 0


class TestErrorHandling:
    def test_invalid_base64_rejected(self):
        resp = _post_recognize("not-valid-base64!!!")
        assert resp.status_code == 400

    def test_missing_api_key_rejected(self):
        resp = httpx.post(
            _url("/hal/api/dl/ser/recognize"),
            json={"audio_b64": _wav_to_b64(HAPPY_WAV)},
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        assert resp.status_code == 401
