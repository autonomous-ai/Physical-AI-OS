"""Tests for client-side RSA+AES-GCM crypto session."""

import json
import os

import pytest
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric import rsa

from hal.drivers.sensing.crypto import (
    CipherHTTPRequest,
    CipherHTTPResponse,
    CryptoSession,
    WSCipherMessage,
    load_public_key,
)


@pytest.fixture()
def rsa_keypair():
    """Generate an in-memory RSA key pair for testing."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    return private_key, public_key


@pytest.fixture()
def public_key(rsa_keypair):
    return rsa_keypair[1]


@pytest.fixture()
def private_key(rsa_keypair):
    return rsa_keypair[0]


class TestCryptoSession:
    def test_encrypt_decrypt_round_trip(self, public_key, private_key):
        session = CryptoSession(public_key)
        original = b"hello world"

        encrypted = session.encrypt(original)
        decrypted = session.decrypt(encrypted)
        assert decrypted == original

    def test_different_nonce_per_encrypt(self, public_key):
        session = CryptoSession(public_key)

        enc1 = session.encrypt(b"same data")
        enc2 = session.encrypt(b"same data")
        assert enc1.nonce != enc2.nonce
        assert enc1.cipher_data != enc2.cipher_data

    def test_tampered_ciphertext_fails(self, public_key):
        import base64
        from hal.drivers.sensing.crypto import CipherPayload

        session = CryptoSession(public_key)
        encrypted = session.encrypt(b"secret")

        raw_cipher = base64.b64decode(encrypted.cipher_data)
        tampered_raw = raw_cipher + b"\xff"
        tampered = CipherPayload(
            nonce=encrypted.nonce,
            cipher_data=base64.b64encode(tampered_raw).decode(),
        )
        with pytest.raises(InvalidTag):
            session.decrypt(tampered)

    def test_wrap_http_request(self, public_key):
        session = CryptoSession(public_key)
        plain = json.dumps({"image_b64": "abc"}).encode()

        wrapped = session.wrap_http_request(plain)
        req = CipherHTTPRequest.model_validate_json(wrapped)
        assert req.encrypted_key == session.encrypted_key_b64
        assert req.nonce
        assert req.cipher_data

    def test_wrap_unwrap_http_round_trip(self, public_key):
        session = CryptoSession(public_key)
        plain = json.dumps({"test": "data"}).encode()

        encrypted = session.encrypt(plain)
        resp = CipherHTTPResponse(nonce=encrypted.nonce, cipher_data=encrypted.cipher_data)
        resp_bytes = resp.model_dump_json().encode()

        decrypted = session.unwrap_http_response(resp_bytes)
        assert decrypted == plain

    def test_wrap_unwrap_ws_round_trip(self, public_key):
        session = CryptoSession(public_key)
        plain = json.dumps({"type": "frame", "task": "pose", "frame_b64": "abc"})

        wrapped = session.wrap_ws_message(plain)
        msg = WSCipherMessage.model_validate_json(wrapped)
        assert msg.type == "encrypted"

        unwrapped = session.unwrap_ws_message(wrapped)
        assert unwrapped == plain

    def test_large_payload(self, public_key):
        session = CryptoSession(public_key)
        original = os.urandom(1_000_000)

        encrypted = session.encrypt(original)
        decrypted = session.decrypt(encrypted)
        assert decrypted == original


class TestLoadPublicKey:
    def test_load_from_pem(self, rsa_keypair):
        from cryptography.hazmat.primitives import serialization

        _, pub = rsa_keypair
        pem = pub.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

        loaded = load_public_key(pem)
        assert loaded.key_size == pub.key_size
