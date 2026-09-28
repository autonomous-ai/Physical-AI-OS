"""HTTP integration tests for /audio-recognizer/embed.

Requires DL_BACKEND_URL, DL_API_KEY and tests/fixtures/audio/; skipped if the endpoint is unavailable.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

import httpx
import numpy as np
import pytest
from dotenv import load_dotenv

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "").rstrip("/")
DL_API_KEY = os.getenv("DL_API_KEY", "")

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures" / "audio"

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL,
    reason="DL_BACKEND_URL not set - skipping audio embedder API tests.",
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


def _post_embed(audios_b64: list[str], use_sliding_window: bool = True) -> httpx.Response:
    payload = {"audios_b64": audios_b64, "use_sliding_window": use_sliding_window}
    return httpx.post(
        _url("/hal/api/dl/audio-recognizer/embed"),
        json=payload,
        headers=_headers(),
        timeout=30.0,
    )


def _embed_file(path: Path, use_sliding_window: bool = True) -> dict:
    resp = _post_embed([_wav_to_b64(path)], use_sliding_window=use_sliding_window)
    assert resp.status_code == 200, f"status={resp.status_code} body={resp.text}"
    return resp.json()


def _api_available() -> bool:
    try:
        resp = _post_embed([_wav_to_b64(next((FIXTURES_DIR / "speaker_a").glob("*.wav")))])
        return resp.status_code != 404
    except Exception:
        return False


@pytest.fixture(scope="session", autouse=True)
def check_api():
    if not FIXTURES_DIR.exists():
        pytest.skip("Audio fixtures not found")
    if not _api_available():
        pytest.skip("Audio embedder endpoint not available (404)")


class TestBasicResponse:
    def test_embed_returns_200(self):
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        resp = _post_embed([_wav_to_b64(wav)])
        assert resp.status_code == 200

    def test_response_shape(self):
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        data = _embed_file(wav)
        assert "embedding" in data
        assert "embedding_dim" in data
        assert data["embedding_dim"] > 0
        assert len(data["embedding"]) == data["embedding_dim"]

    def test_l2_normalized(self):
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        data = _embed_file(wav)
        emb = np.array(data["embedding"], dtype=np.float32)
        assert abs(np.linalg.norm(emb) - 1.0) < 1e-3

    def test_single_shot_returns_no_chunks(self):
        # Enroll path: whole utterance in one shot, no per-chunk matrix.
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        data = _embed_file(wav, use_sliding_window=False)
        assert data.get("chunk_embeddings") is None

    def test_sliding_window_returns_chunks(self):
        # Recognize path: windowed, per-chunk matrix returned for voting.
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        data = _embed_file(wav, use_sliding_window=True)
        assert data["chunk_embeddings"] is not None
        assert len(data["chunk_embeddings"]) >= 1
        for chunk in data["chunk_embeddings"]:
            assert len(chunk) == data["embedding_dim"]

    def test_sliding_window_defaults_true(self):
        # A request that omits the flag defaults to windowed (recognize) mode.
        wav = next((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        resp = httpx.post(
            _url("/hal/api/dl/audio-recognizer/embed"),
            json={"audios_b64": [_wav_to_b64(wav)]},
            headers=_headers(),
            timeout=30.0,
        )
        assert resp.status_code == 200
        assert resp.json().get("chunk_embeddings") is not None


class TestSpeakerDiscrimination:
    def _cosine(self, a: list[float], b: list[float]) -> float:
        a_np = np.array(a, dtype=np.float32)
        b_np = np.array(b, dtype=np.float32)
        return float(np.dot(a_np, b_np))

    def test_same_person_higher_than_different(self):
        a_files = sorted((FIXTURES_DIR / "speaker_a").glob("*.wav"))
        b_files = sorted((FIXTURES_DIR / "speaker_b").glob("*.wav"))

        a_embs = [_embed_file(f)["embedding"] for f in a_files]
        b_embs = [_embed_file(f)["embedding"] for f in b_files]

        same_sims = []
        for i in range(len(a_embs)):
            for j in range(i + 1, len(a_embs)):
                same_sims.append(self._cosine(a_embs[i], a_embs[j]))
        for i in range(len(b_embs)):
            for j in range(i + 1, len(b_embs)):
                same_sims.append(self._cosine(b_embs[i], b_embs[j]))

        diff_sims = []
        for a in a_embs:
            for b in b_embs:
                diff_sims.append(self._cosine(a, b))

        avg_same = np.mean(same_sims)
        avg_diff = np.mean(diff_sims)
        assert avg_same > avg_diff, (
            f"Same-person avg ({avg_same:.4f}) should exceed "
            f"different-person avg ({avg_diff:.4f})"
        )


class TestErrorHandling:
    def test_empty_audios_rejected(self):
        resp = httpx.post(
            _url("/hal/api/dl/audio-recognizer/embed"),
            json={"audios_b64": []},
            headers=_headers(),
            timeout=10.0,
        )
        assert resp.status_code == 422

    def test_invalid_base64_rejected(self):
        resp = _post_embed(["not-valid-base64!!!"])
        assert resp.status_code == 400

    def test_missing_api_key_rejected(self):
        resp = httpx.post(
            _url("/hal/api/dl/audio-recognizer/embed"),
            json={"audios_b64": [_wav_to_b64(next((FIXTURES_DIR / "speaker_a").glob("*.wav")))]},
            headers={"Content-Type": "application/json"},
            timeout=10.0,
        )
        assert resp.status_code == 401
