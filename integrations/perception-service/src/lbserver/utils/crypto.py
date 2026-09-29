"""Crypto helpers for the load balancer HTTP proxy."""

from binascii import Error as BinAsciiError

from cryptography.exceptions import InvalidTag
from fastapi import HTTPException
from pydantic import ValidationError

from config import settings
from core.models.crypto import AESGCMPlainPayload
from lbserver.models import CipherHTTPRequest, CipherHTTPResponse
from lbserver.utils.state import get_crypto


def try_decrypt_http_body(body: bytes) -> tuple[bytes, bytes | None]:
    """If body is an CipherHTTPRequest, decrypt it.

    Returns:
        (plain_body, encrypted_key) — encrypted_key is set if decrypted, None if plain.
    """
    crypto = get_crypto()
    if crypto is None:
        return body, None

    try:
        req = CipherHTTPRequest.model_validate_json(body)
    except (ValidationError, ValueError):
        # Plaintext is accepted unless require_encryption is set (dev/back-compat path).
        if settings.crypto.require_encryption:
            raise HTTPException(status_code=400, detail="Encryption required")
        return body, None

    payload, encrypted_key = req.to_raw_payload()

    try:
        session = crypto.create_session(encrypted_key)
        result = session.decrypt(payload)
        return result.plain_data, encrypted_key
    except InvalidTag:
        raise HTTPException(
            status_code=400,
            detail="Decryption failed: invalid auth tag (wrong key or tampered data)",
        )
    except (ValueError, BinAsciiError) as e:
        raise HTTPException(status_code=400, detail=f"Decryption failed: {e}")


def encrypt_http_response(content: bytes, encrypted_key: bytes) -> bytes:
    """Encrypt response body using the same session key as the request."""
    crypto = get_crypto()
    if crypto is None:
        raise RuntimeError("Cannot encrypt response: crypto not initialized")

    session = crypto.create_session(encrypted_key)
    encrypted = session.encrypt(AESGCMPlainPayload(plain_data=content))
    resp = CipherHTTPResponse.from_raw_payload(encrypted)
    return resp.model_dump_json().encode()
