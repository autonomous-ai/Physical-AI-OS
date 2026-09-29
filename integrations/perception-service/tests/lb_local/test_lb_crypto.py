"""Tests for the LB encryption pipeline (HTTP and WS) with mocked backends and in-memory keys."""

import base64
import json
import os
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.testclient import TestClient

from core.crypto.rsa_aes import AESGCMSession, RSAAESCrypto
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import (
    CipherHTTPRequest,
    CipherHTTPResponse,
    WSCipherMessage,
)

def _make_session(crypto: RSAAESCrypto) -> tuple[AESGCMSession, bytes]:
    """Generate a session key, RSA-encrypt it, return (session, encrypted_key_bytes)."""
    session_key = os.urandom(32)
    encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)
    return AESGCMSession(session_key), encrypted_key


def _encrypt_body(session: AESGCMSession, encrypted_key: bytes, plain: bytes) -> bytes:
    """Build an CipherHTTPRequest JSON from plain bytes."""
    encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain))
    req = CipherHTTPRequest(
        encrypted_key=base64.b64encode(encrypted_key).decode(),
        nonce=base64.b64encode(encrypted.nonce).decode(),
        cipher_data=base64.b64encode(encrypted.cipher_data).decode(),
    )
    return req.model_dump_json().encode()


def _decrypt_response(session: AESGCMSession, content: bytes) -> bytes:
    """Decrypt an CipherHTTPResponse JSON back to plain bytes."""
    enc_resp = CipherHTTPResponse.model_validate_json(content)
    payload = enc_resp.to_raw_payload()
    return session.decrypt(payload).plain_data


def _mock_response(request: httpx.Request) -> httpx.Response:
    """Mock backend that echoes the request body."""
    return httpx.Response(
        200,
        json={
            "path": str(request.url.path),
            "body": request.content.decode() if request.content else "",
        },
    )


@pytest.fixture()
def crypto() -> RSAAESCrypto:
    return RSAAESCrypto()


def _patch_httpx_mock(monkeypatch) -> None:
    """Monkeypatch httpx.AsyncClient in lbserver.app to use MockTransport."""
    _real_async_client = httpx.AsyncClient

    def _mocked_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(_mock_response)
        return _real_async_client(**kwargs)

    monkeypatch.setattr("lbserver.app.httpx.AsyncClient", _mocked_client)


@pytest.fixture()
def lb_client(monkeypatch, crypto):
    """LB TestClient with crypto enabled and mocked HTTP backend."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    monkeypatch.setattr("lbserver.app.BACKENDS", ["http://backend:8001"])
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(["http://backend:8001"]))
    _patch_httpx_mock(monkeypatch)
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


class TestPublicKeyEndpoint:
    def test_returns_valid_pem(self, lb_client, crypto):
        resp = lb_client.get("/api/crypto/public-key")
        assert resp.status_code == 200
        pem = resp.text
        assert pem.startswith("-----BEGIN PUBLIC KEY-----")
        assert pem.strip().endswith("-----END PUBLIC KEY-----")
        assert pem == crypto.public_key_pem

    def test_returns_404_when_disabled(self, monkeypatch):
        from lbserver.utils.state import set_crypto

        set_crypto(None)
        from lbserver.app import app
        client = TestClient(app)
        resp = client.get("/api/crypto/public-key")
        assert resp.status_code == 404


class TestHTTPEncryption:
    def test_encrypted_request_decrypted_before_forwarding(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        plain_body = json.dumps({"image_b64": "abc123"}).encode()
        encrypted_body = _encrypt_body(session, encrypted_key, plain_body)

        resp = lb_client.post("/api/dl/emotion-recognize", content=encrypted_body)
        assert resp.status_code == 200

        # Response is encrypted
        decrypted = _decrypt_response(session, resp.content)
        data = json.loads(decrypted)
        # Backend received the plain body
        assert json.loads(data["body"]) == {"image_b64": "abc123"}

    def test_full_round_trip(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        plain = json.dumps({"test": "hello"}).encode()
        encrypted_body = _encrypt_body(session, encrypted_key, plain)

        resp = lb_client.post("/api/dl/test", content=encrypted_body)
        assert resp.status_code == 200

        decrypted = _decrypt_response(session, resp.content)
        data = json.loads(decrypted)
        assert data["path"] == "/api/dl/test"
        assert json.loads(data["body"]) == {"test": "hello"}

    def test_plain_request_passes_through(self, lb_client):
        resp = lb_client.post("/api/dl/test", json={"plain": True})
        assert resp.status_code == 200
        # Response is plain (not encrypted)
        data = resp.json()
        assert "path" in data

    def test_tampered_ciphertext_returns_400(self, lb_client, crypto):
        session, encrypted_key = _make_session(crypto)
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=b"data"))

        req = CipherHTTPRequest(
            encrypted_key=base64.b64encode(encrypted_key).decode(),
            nonce=base64.b64encode(encrypted.nonce).decode(),
            cipher_data=base64.b64encode(encrypted.cipher_data + b"\xff").decode(),
        )
        resp = lb_client.post("/api/dl/test", content=req.model_dump_json().encode())
        assert resp.status_code == 400
        assert "invalid auth tag" in resp.json()["detail"]


WS_ECHO_PORT = 19050


def _make_ws_echo_app() -> FastAPI:
    """A tiny WS echo server that sends back whatever it receives."""
    ws_app = FastAPI()

    @ws_app.websocket("/{path:path}")
    async def echo(ws: WebSocket, path: str):
        await ws.accept()
        try:
            while True:
                data = await ws.receive_text()
                await ws.send_text(data)
        except WebSocketDisconnect:
            pass

    return ws_app


@pytest.fixture(scope="module", autouse=True)
def ws_echo_server():
    """Start a WS echo backend for the test module."""
    app = _make_ws_echo_app()
    t = threading.Thread(
        target=uvicorn.run,
        kwargs={"app": app, "host": "127.0.0.1", "port": WS_ECHO_PORT, "log_level": "error"},
        daemon=True,
    )
    t.start()
    time.sleep(0.5)
    yield


@pytest.fixture()
def lb_ws_client(monkeypatch, crypto):
    """LB TestClient with crypto enabled, WS backend pointing to echo server."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    backends = [f"http://127.0.0.1:{WS_ECHO_PORT}"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    monkeypatch.setattr("lbserver.app.ws_rr", RoundRobin(backends))
    _patch_httpx_mock(monkeypatch)
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


class TestWSEncryption:
    def test_key_exchange(self, lb_ws_client, crypto):
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({
                "type": "key_exchange",
                "encrypted_key": base64.b64encode(encrypted_key).decode(),
            })
            resp = ws.receive_json()
            assert resp["status"] == "key_exchange_ok"

    def test_encrypted_message_round_trip(self, lb_ws_client, crypto):
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)
        session = AESGCMSession(session_key)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({
                "type": "key_exchange",
                "encrypted_key": base64.b64encode(encrypted_key).decode(),
            })
            resp = ws.receive_json()
            assert resp["status"] == "key_exchange_ok"

            plain = json.dumps({"type": "frame", "task": "pose", "frame_b64": "abc"})
            encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain.encode()))
            ws_msg = WSCipherMessage.from_raw_payload(encrypted)
            ws.send_text(ws_msg.model_dump_json())

            raw_resp = ws.receive_text()
            enc_resp = WSCipherMessage.model_validate_json(raw_resp)
            decrypted = session.decrypt(enc_resp.to_raw_payload())
            assert json.loads(decrypted.plain_data) == json.loads(plain)

    def test_plain_ws_without_key_exchange(self, lb_ws_client):
        """Without key exchange, messages pass through unencrypted."""
        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            # Plain message, no key exchange.
            ws.send_json({"type": "heartbeat", "task": "pose"})
            resp = ws.receive_json()
            assert resp == {"type": "heartbeat", "task": "pose"}


GCM_NONCE_SIZE = 12


class _ClientCryptoSession:
    """Standalone client-side crypto — no server imports, only cryptography lib."""

    def __init__(self, public_key_pem: str) -> None:
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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
        nonce = base64.b64decode(nonce_b64)
        cipher_data = base64.b64decode(cipher_data_b64)
        return self._aesgcm.decrypt(nonce, cipher_data, None)

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


@pytest.fixture()
def lb_client_require_encryption(monkeypatch, crypto):
    """LB TestClient with crypto enabled and require_encryption=true."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    monkeypatch.setattr("lbserver.app.BACKENDS", ["http://backend:8001"])
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(["http://backend:8001"]))
    _patch_httpx_mock(monkeypatch)
    monkeypatch.setattr("config.settings.crypto.require_encryption", True)
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


@pytest.fixture()
def lb_ws_client_require_encryption(monkeypatch, crypto):
    """LB WS TestClient with crypto enabled and require_encryption=true."""
    from lbserver.utils import RoundRobin
    from lbserver.utils.state import set_crypto

    backends = [f"http://127.0.0.1:{WS_ECHO_PORT}"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    monkeypatch.setattr("lbserver.app.ws_rr", RoundRobin(backends))
    _patch_httpx_mock(monkeypatch)
    monkeypatch.setattr("config.settings.crypto.require_encryption", True)
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


class TestRequireEncryption:
    """When require_encryption=true, plaintext requests must be rejected."""

    def test_plain_http_rejected(self, lb_client_require_encryption):
        resp = lb_client_require_encryption.post(
            "/api/dl/test",
            json={"image_b64": "plain_data"},
        )
        assert resp.status_code == 400
        assert "Encryption required" in resp.json()["detail"]

    def test_encrypted_http_still_works(self, lb_client_require_encryption):
        resp = lb_client_require_encryption.get("/api/crypto/public-key")
        session = _ClientCryptoSession(resp.text)

        plain = json.dumps({"test": "data"}).encode()
        resp = lb_client_require_encryption.post(
            "/api/dl/test", content=session.wrap_http_request(plain)
        )
        assert resp.status_code == 200
        decrypted = json.loads(session.unwrap_http_response(resp.content))
        assert json.loads(decrypted["body"]) == {"test": "data"}

    def test_ws_without_key_exchange_closed(self, lb_ws_client_require_encryption):

        with pytest.raises(Exception):
            with lb_ws_client_require_encryption.websocket_connect("/api/dl/test/ws") as ws:
                ws.send_json({"type": "frame", "task": "pose", "frame_b64": "abc"})
                ws.receive_text()

    def test_ws_with_key_exchange_works(self, lb_ws_client_require_encryption):
        resp = lb_ws_client_require_encryption.get("/api/crypto/public-key")
        session = _ClientCryptoSession(resp.text)

        with lb_ws_client_require_encryption.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({"type": "key_exchange", "encrypted_key": session.encrypted_key_b64})
            assert ws.receive_json()["status"] == "key_exchange_ok"

            plain = json.dumps({"type": "frame", "task": "pose", "frame_b64": "abc"})
            ws.send_text(session.wrap_ws_message(plain))
            decrypted = json.loads(session.unwrap_ws_message(ws.receive_text()))
            assert decrypted["frame_b64"] == "abc"


class TestClientHTTP:
    """Simulate a client: fetch public key, encrypt request, decrypt response."""

    def test_fetch_key_and_round_trip(self, lb_client):
        resp = lb_client.get("/api/crypto/public-key")
        assert resp.status_code == 200
        session = _ClientCryptoSession(resp.text)

        plain = json.dumps({"image_b64": "abc123", "threshold": 0.5}).encode()
        resp = lb_client.post("/api/dl/emotion-recognize", content=session.wrap_http_request(plain))
        assert resp.status_code == 200

        decrypted = json.loads(session.unwrap_http_response(resp.content))
        assert json.loads(decrypted["body"]) == {"image_b64": "abc123", "threshold": 0.5}

    def test_multiple_requests_same_session(self, lb_client):
        resp = lb_client.get("/api/crypto/public-key")
        session = _ClientCryptoSession(resp.text)

        for i in range(3):
            plain = json.dumps({"request_id": i}).encode()
            resp = lb_client.post("/api/dl/test", content=session.wrap_http_request(plain))
            assert resp.status_code == 200
            decrypted = json.loads(session.unwrap_http_response(resp.content))
            assert json.loads(decrypted["body"]) == {"request_id": i}


class TestClientWS:
    """Simulate a client: fetch key, WS key exchange, encrypted frames."""

    def test_full_ws_flow(self, lb_ws_client):
        resp = lb_ws_client.get("/api/crypto/public-key")
        session = _ClientCryptoSession(resp.text)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({"type": "key_exchange", "encrypted_key": session.encrypted_key_b64})
            assert ws.receive_json()["status"] == "key_exchange_ok"

            plain = json.dumps({"type": "frame", "task": "pose", "frame_b64": "img_data"})
            ws.send_text(session.wrap_ws_message(plain))

            decrypted = session.unwrap_ws_message(ws.receive_text())
            data = json.loads(decrypted)
            assert data["type"] == "frame"
            assert data["frame_b64"] == "img_data"

    def test_multiple_frames(self, lb_ws_client):
        resp = lb_ws_client.get("/api/crypto/public-key")
        session = _ClientCryptoSession(resp.text)

        with lb_ws_client.websocket_connect("/api/dl/test/ws") as ws:
            ws.send_json({"type": "key_exchange", "encrypted_key": session.encrypted_key_b64})
            assert ws.receive_json()["status"] == "key_exchange_ok"

            for i in range(5):
                plain = json.dumps({"type": "frame", "task": "action", "frame_b64": f"f{i}"})
                ws.send_text(session.wrap_ws_message(plain))
                decrypted = json.loads(session.unwrap_ws_message(ws.receive_text()))
                assert decrypted["frame_b64"] == f"f{i}"
