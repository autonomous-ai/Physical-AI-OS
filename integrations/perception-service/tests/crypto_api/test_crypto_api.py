"""Integration tests: encrypted API calls through the LB (CRYPTO__ENABLED=true) to a live DL backend.

Requires DL_BACKEND_URL and DL_API_KEY. Require-encryption tests need CRYPTO_REQUIRE_ENCRYPTION=true.
"""

import base64
import json
import os

import cv2
import httpx
import numpy as np
import pytest
import pytest_asyncio
import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from dotenv import load_dotenv

_ = load_dotenv(override=True)

DL_BACKEND_URL = os.getenv("DL_BACKEND_URL", "").rstrip("/")
DL_API_KEY = os.getenv("DL_API_KEY", "")
GCM_NONCE_SIZE = 12

pytestmark = pytest.mark.skipif(
    not DL_BACKEND_URL, reason="DL_BACKEND_URL not set — skipping remote API tests"
)


class CryptoSession:
    """Client-side AES-256-GCM session using the server's RSA public key."""

    def __init__(self, public_key_pem: str) -> None:
        public_key = serialization.load_pem_public_key(public_key_pem.encode())
        self._session_key = os.urandom(32)
        self._aesgcm = AESGCM(self._session_key)
        oaep = padding.OAEP(
            mgf=padding.MGF1(hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        )
        self._encrypted_key = public_key.encrypt(self._session_key, oaep)
        self.encrypted_key_b64 = base64.b64encode(self._encrypted_key).decode()

    def encrypt(self, plaintext: bytes) -> dict:
        nonce = os.urandom(GCM_NONCE_SIZE)
        cipher_data = self._aesgcm.encrypt(nonce, plaintext, None)
        return {
            "nonce": base64.b64encode(nonce).decode(),
            "cipher_data": base64.b64encode(cipher_data).decode(),
        }

    def decrypt_fields(self, nonce_b64: str, cipher_data_b64: str) -> bytes:
        return self._aesgcm.decrypt(
            base64.b64decode(nonce_b64),
            base64.b64decode(cipher_data_b64),
            None,
        )

    def wrap_http_request(self, plain_body: bytes) -> bytes:
        enc = self.encrypt(plain_body)
        return json.dumps({"encrypted_key": self.encrypted_key_b64, **enc}).encode()

    def unwrap_http_response(self, resp_body: bytes) -> bytes:
        data = json.loads(resp_body)
        return self.decrypt_fields(data["nonce"], data["cipher_data"])

    def wrap_ws_message(self, plain: str) -> str:
        enc = self.encrypt(plain.encode())
        return json.dumps({"type": "encrypted", **enc})

    def unwrap_ws_message(self, raw: str) -> str:
        msg = json.loads(raw)
        return self.decrypt_fields(msg["nonce"], msg["cipher_data"]).decode()


def _http_url(path: str) -> str:
    return f"{DL_BACKEND_URL}{path}"


def _ws_url(path: str) -> str:
    return DL_BACKEND_URL.replace("http://", "ws://").replace("https://", "wss://") + path


AUTH_HEADERS = {"X-API-Key": DL_API_KEY}


def _make_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.random.randint(0, 255, (height, width, 3), dtype=np.uint8)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


def _make_face_frame_b64(width: int = 320, height: int = 240) -> str:
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    center = (width // 2, height // 2)
    cv2.ellipse(frame, center, (50, 65), 0, 0, 360, (200, 180, 170), -1)
    cv2.circle(frame, (center[0] - 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.circle(frame, (center[0] + 20, center[1] - 15), 5, (40, 40, 40), -1)
    cv2.ellipse(frame, (center[0], center[1] + 25), (15, 8), 0, 0, 180, (40, 40, 80), -1)
    _, buf = cv2.imencode(".jpg", frame)
    return base64.b64encode(buf.tobytes()).decode()


@pytest.fixture(scope="module")
def crypto_session() -> CryptoSession:
    """Fetch public key from LB and create a client crypto session."""
    resp = httpx.get(_http_url("/api/crypto/public-key"), headers=AUTH_HEADERS)
    if resp.status_code != 200:
        pytest.skip("Public key endpoint not available (crypto disabled on LB?)")
    return CryptoSession(resp.text)


class TestPublicKey:
    def test_returns_valid_pem(self):
        resp = httpx.get(_http_url("/api/crypto/public-key"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.text.startswith("-----BEGIN PUBLIC KEY-----")
        assert resp.text.strip().endswith("-----END PUBLIC KEY-----")


class TestEncryptedEmotionHTTP:
    def test_encrypted_emotion_recognize(self, crypto_session):
        plain = json.dumps({
            "image_b64": _make_face_frame_b64(),
            "threshold": 0.3,
        }).encode()

        resp = httpx.post(
            _http_url("/hal/api/dl/emotion-recognize"),
            content=crypto_session.wrap_http_request(plain),
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            timeout=15.0,
        )
        assert resp.status_code == 200

        decrypted = json.loads(crypto_session.unwrap_http_response(resp.content))
        assert "detections" in decrypted
        assert isinstance(decrypted["detections"], list)

    def test_encrypted_emotion_with_high_threshold(self, crypto_session):
        plain = json.dumps({
            "image_b64": _make_face_frame_b64(),
            "threshold": 1.0,
        }).encode()

        resp = httpx.post(
            _http_url("/hal/api/dl/emotion-recognize"),
            content=crypto_session.wrap_http_request(plain),
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            timeout=15.0,
        )
        assert resp.status_code == 200

        decrypted = json.loads(crypto_session.unwrap_http_response(resp.content))
        assert decrypted["detections"] == []


class TestEncryptedActionWS:
    @pytest_asyncio.fixture()
    async def ws_session(self, crypto_session):
        """Connect WS, perform key exchange, yield (ws, crypto_session)."""
        async with websockets.connect(
            _ws_url("/hal/api/dl/action-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "key_exchange",
                "encrypted_key": crypto_session.encrypted_key_b64,
            }))
            resp = json.loads(await ws.recv())
            assert resp["status"] == "key_exchange_ok"
            yield ws, crypto_session

    @pytest.mark.asyncio
    async def test_encrypted_config_and_frame(self, ws_session):
        ws, session = ws_session

        config_msg = json.dumps({
            "type": "config",
            "task": "action",
            "whitelist": None,
            "threshold": 0.3,
        })
        await ws.send(session.wrap_ws_message(config_msg))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert resp["status"] == "config_updated"

        frame_msg = json.dumps({
            "type": "frame",
            "task": "action",
            "frame_b64": _make_frame_b64(),
        })
        await ws.send(session.wrap_ws_message(frame_msg))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert "detected_classes" in resp

    @pytest.mark.asyncio
    async def test_encrypted_multiple_frames(self, ws_session):
        ws, session = ws_session

        for _ in range(3):
            frame_msg = json.dumps({
                "type": "frame",
                "task": "action",
                "frame_b64": _make_frame_b64(),
            })
            await ws.send(session.wrap_ws_message(frame_msg))
            raw = await ws.recv()
            resp = json.loads(session.unwrap_ws_message(raw))
            assert "detected_classes" in resp


class TestEncryptedEmotionWS:
    @pytest_asyncio.fixture()
    async def ws_session(self, crypto_session):
        async with websockets.connect(
            _ws_url("/hal/api/dl/emotion-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "key_exchange",
                "encrypted_key": crypto_session.encrypted_key_b64,
            }))
            resp = json.loads(await ws.recv())
            assert resp["status"] == "key_exchange_ok"
            yield ws, crypto_session

    @pytest.mark.asyncio
    async def test_encrypted_emotion_frame(self, ws_session):
        ws, session = ws_session

        frame_msg = json.dumps({
            "type": "frame",
            "task": "emotion",
            "frame_b64": _make_face_frame_b64(),
        })
        await ws.send(session.wrap_ws_message(frame_msg))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert "detections" in resp

    @pytest.mark.asyncio
    async def test_encrypted_heartbeat(self, ws_session):
        ws, session = ws_session

        hb = json.dumps({"type": "heartbeat", "task": "emotion"})
        await ws.send(session.wrap_ws_message(hb))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert resp == {"status": "ok"}


class TestEncryptedPoseWS:
    @pytest_asyncio.fixture()
    async def ws_session(self, crypto_session):
        async with websockets.connect(
            _ws_url("/hal/api/dl/pose-estimation/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "key_exchange",
                "encrypted_key": crypto_session.encrypted_key_b64,
            }))
            resp = json.loads(await ws.recv())
            assert resp["status"] == "key_exchange_ok"
            yield ws, crypto_session

    @pytest.mark.asyncio
    async def test_encrypted_pose_frame(self, ws_session):
        ws, session = ws_session

        frame_msg = json.dumps({
            "type": "frame",
            "task": "pose",
            "frame_b64": _make_frame_b64(),
        })
        await ws.send(session.wrap_ws_message(frame_msg))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert "pose_2d" in resp
        assert "joints" in resp["pose_2d"]

    @pytest.mark.asyncio
    async def test_encrypted_pose_multiple_frames(self, ws_session):
        ws, session = ws_session

        for _ in range(3):
            frame_msg = json.dumps({
                "type": "frame",
                "task": "pose",
                "frame_b64": _make_frame_b64(),
            })
            await ws.send(session.wrap_ws_message(frame_msg))
            raw = await ws.recv()
            resp = json.loads(session.unwrap_ws_message(raw))
            assert "pose_2d" in resp

    @pytest.mark.asyncio
    async def test_encrypted_pose_heartbeat(self, ws_session):
        ws, session = ws_session

        hb = json.dumps({"type": "heartbeat", "task": "pose"})
        await ws.send(session.wrap_ws_message(hb))
        raw = await ws.recv()
        resp = json.loads(session.unwrap_ws_message(raw))
        assert resp == {"status": "ok"}


REQUIRE_ENCRYPTION = os.getenv("CRYPTO_REQUIRE_ENCRYPTION", "").lower() in ("1", "true", "yes")

require_encryption_mark = pytest.mark.skipif(
    not REQUIRE_ENCRYPTION,
    reason="CRYPTO_REQUIRE_ENCRYPTION not set — skipping require-encryption tests",
)


@require_encryption_mark
class TestRequireEncryptionHTTP:
    """When require_encryption=true, plaintext HTTP requests must be rejected."""

    def test_plain_post_rejected(self):
        resp = httpx.post(
            _http_url("/hal/api/dl/emotion-recognize"),
            json={"image_b64": _make_face_frame_b64(), "threshold": 0.5},
            headers=AUTH_HEADERS,
            timeout=15.0,
        )
        assert resp.status_code == 400
        assert "Encryption required" in resp.json()["detail"]

    def test_encrypted_post_accepted(self, crypto_session):
        plain = json.dumps({"image_b64": _make_face_frame_b64(), "threshold": 0.5}).encode()
        resp = httpx.post(
            _http_url("/hal/api/dl/emotion-recognize"),
            content=crypto_session.wrap_http_request(plain),
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
            timeout=15.0,
        )
        assert resp.status_code == 200

    def test_get_requests_unaffected(self):
        """GET /api/dl/health should still work without encryption."""
        resp = httpx.get(_http_url("/hal/api/dl/health"), headers=AUTH_HEADERS)
        assert resp.status_code == 200

    def test_public_key_still_accessible(self):
        resp = httpx.get(_http_url("/api/crypto/public-key"), headers=AUTH_HEADERS)
        assert resp.status_code == 200
        assert resp.text.startswith("-----BEGIN PUBLIC KEY-----")


@require_encryption_mark
class TestRequireEncryptionWS:
    """When require_encryption=true, WS without key exchange must be closed."""

    @pytest.mark.asyncio
    async def test_ws_without_key_exchange_closed(self):
        with pytest.raises(Exception):
            async with websockets.connect(
                _ws_url("/hal/api/dl/action-analysis/ws"),
                additional_headers=AUTH_HEADERS,
            ) as ws:
                await ws.send(json.dumps({
                    "type": "frame",
                    "task": "action",
                    "frame_b64": _make_frame_b64(),
                }))
                await ws.recv()

    @pytest.mark.asyncio
    async def test_ws_with_key_exchange_works(self, crypto_session):
        async with websockets.connect(
            _ws_url("/hal/api/dl/action-analysis/ws"),
            additional_headers=AUTH_HEADERS,
        ) as ws:
            await ws.send(json.dumps({
                "type": "key_exchange",
                "encrypted_key": crypto_session.encrypted_key_b64,
            }))
            resp = json.loads(await ws.recv())
            assert resp["status"] == "key_exchange_ok"

            frame_msg = json.dumps({
                "type": "frame",
                "task": "action",
                "frame_b64": _make_frame_b64(),
            })
            await ws.send(crypto_session.wrap_ws_message(frame_msg))
            raw = await ws.recv()
            resp = json.loads(crypto_session.unwrap_ws_message(raw))
            assert "detected_classes" in resp
