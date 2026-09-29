"""Tests for the load balancer HTTP proxy with a mocked httpx client."""

import base64
import json
import os

import httpx
import pytest
from fastapi.testclient import TestClient

from core.crypto.rsa_aes import RSAAESCrypto
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import CipherHTTPRequest


def _mock_response(request: httpx.Request) -> httpx.Response:
    """Mock backend that echoes request info."""
    backend = f"{request.url.host}:{request.url.port}"
    return httpx.Response(
        200,
        json={
            "backend": backend,
            "path": str(request.url.path),
            "method": request.method,
            "api_key": request.headers.get("x-api-key", ""),
            "body": request.content.decode() if request.content else "",
        },
    )


def _patch_httpx_mock(monkeypatch) -> None:
    """Monkeypatch httpx.AsyncClient in lbserver.app to use MockTransport."""
    _real_async_client = httpx.AsyncClient

    def _mocked_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(_mock_response)
        return _real_async_client(**kwargs)

    monkeypatch.setattr("lbserver.app.httpx.AsyncClient", _mocked_client)


@pytest.fixture()
def lb_client(monkeypatch):
    """Create a TestClient for the LB with mocked backends."""
    from lbserver.utils import RoundRobin

    backends = ["http://gpu1:8001", "http://gpu2:8001"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "/_internal")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    _patch_httpx_mock(monkeypatch)

    from lbserver.app import app
    return TestClient(app)


@pytest.fixture()
def crypto() -> RSAAESCrypto:
    """Create an in-memory RSA+AES crypto instance."""
    return RSAAESCrypto()


@pytest.fixture()
def lb_client_with_crypto(monkeypatch, crypto):
    """LB client with crypto enabled and mocked backend."""
    from lbserver.utils import RoundRobin

    backends = ["http://gpu1:8001"]
    monkeypatch.setattr("lbserver.app.BACKENDS", backends)
    monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")
    monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(backends))
    _patch_httpx_mock(monkeypatch)

    from lbserver.utils.state import set_crypto
    set_crypto(crypto)

    from lbserver.app import app
    client = TestClient(app)
    yield client

    set_crypto(None)


class TestHTTPProxy:
    def test_round_robin_alternates(self, lb_client):
        resp1 = lb_client.get("/some/path")
        resp2 = lb_client.get("/some/path")
        assert resp1.status_code == 200
        assert resp2.status_code == 200

        b1 = resp1.json()["backend"]
        b2 = resp2.json()["backend"]
        assert b1 != b2

    def test_internal_prefix_prepended(self, lb_client):
        resp = lb_client.get("/hal/api/dl/health")
        assert resp.status_code == 200
        assert resp.json()["path"] == "/_internal/hal/api/dl/health"

    def test_method_forwarded(self, lb_client):
        resp = lb_client.post("/api/dl/detect", content=b"test")
        assert resp.status_code == 200
        assert resp.json()["method"] == "POST"

    def test_headers_forwarded(self, lb_client):
        resp = lb_client.get("/test", headers={"X-API-Key": "secret123"})
        assert resp.status_code == 200
        assert resp.json()["api_key"] == "secret123"

    def test_full_cycle(self, lb_client):
        backends = []
        for _ in range(4):
            resp = lb_client.get("/ping")
            backends.append(resp.json()["backend"])
        assert backends[0] == backends[2]
        assert backends[1] == backends[3]
        assert backends[0] != backends[1]


class TestHTTPProxyErrors:
    def test_unreachable_backend_returns_502(self, monkeypatch):
        from lbserver.utils import RoundRobin

        monkeypatch.setattr("lbserver.app.http_rr", RoundRobin(["http://127.0.0.1:19999"]))
        monkeypatch.setattr("lbserver.app.INTERNAL_PREFIX", "")

        from lbserver.app import app
        client = TestClient(app)
        resp = client.get("/anything")
        assert resp.status_code == 502


class TestPublicKeyEndpoint:
    def test_returns_pem_when_crypto_enabled(self, lb_client_with_crypto, crypto):
        resp = lb_client_with_crypto.get("/api/crypto/public-key")
        assert resp.status_code == 200
        assert resp.text.startswith("-----BEGIN PUBLIC KEY-----")
        assert resp.text.strip().endswith("-----END PUBLIC KEY-----")
        assert resp.text == crypto.public_key_pem

    def test_returns_404_when_crypto_disabled(self, lb_client):
        from lbserver.utils.state import set_crypto
        set_crypto(None)

        resp = lb_client.get("/api/crypto/public-key")
        assert resp.status_code == 404


class TestHTTPEncryptionPipeline:
    def _make_encrypted_request(self, crypto: RSAAESCrypto, plain_body: bytes) -> bytes:
        """Encrypt a request body using the server's public key."""
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)

        from core.crypto.rsa_aes import AESGCMSession
        session = AESGCMSession(session_key)
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain_body))

        req = CipherHTTPRequest(
            encrypted_key=base64.b64encode(encrypted_key).decode(),
            nonce=base64.b64encode(encrypted.nonce).decode(),
            cipher_data=base64.b64encode(encrypted.cipher_data).decode(),
        )
        return req.model_dump_json().encode()

    def test_encrypted_request_decrypted_before_forwarding(self, lb_client_with_crypto, crypto):
        plain_body = json.dumps({"image_b64": "abc123"}).encode()
        encrypted_body = self._make_encrypted_request(crypto, plain_body)

        resp = lb_client_with_crypto.post(
            "/api/dl/emotion-recognize",
            content=encrypted_body,
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        # Response is encrypted — verify it has the encrypted structure
        resp_data = json.loads(resp.content)
        assert "nonce" in resp_data
        assert "cipher_data" in resp_data

    def test_encrypted_response_decryptable(self, lb_client_with_crypto, crypto):
        """Full round-trip: encrypt request -> LB decrypts -> backend responds -> LB encrypts -> client decrypts."""
        session_key = os.urandom(32)
        encrypted_key = crypto._public_key.encrypt(session_key, RSAAESCrypto.PADDING)

        from core.crypto.rsa_aes import AESGCMSession
        session = AESGCMSession(session_key)

        plain_body = json.dumps({"test": "data"}).encode()
        encrypted = session.encrypt(AESGCMPlainPayload(plain_data=plain_body))

        req = CipherHTTPRequest(
            encrypted_key=base64.b64encode(encrypted_key).decode(),
            nonce=base64.b64encode(encrypted.nonce).decode(),
            cipher_data=base64.b64encode(encrypted.cipher_data).decode(),
        )

        resp = lb_client_with_crypto.post(
            "/api/dl/test",
            content=req.model_dump_json().encode(),
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200

        from lbserver.models import CipherHTTPResponse
        enc_resp = CipherHTTPResponse.model_validate_json(resp.content)
        decrypted = session.decrypt(enc_resp.to_raw_payload())
        resp_json = json.loads(decrypted.plain_data)

        # The mock backend echoes back — verify we got valid JSON
        assert "backend" in resp_json
        assert "path" in resp_json

    def test_plain_request_still_works(self, lb_client_with_crypto):
        """Plain base64 request passes through when require_encryption is off."""
        resp = lb_client_with_crypto.post(
            "/api/dl/test",
            json={"image_b64": "plain_data"},
        )
        assert resp.status_code == 200
        # Response is plain (not encrypted) since request was plain
        data = resp.json()
        assert "backend" in data
