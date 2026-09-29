"""Pydantic models for the load balancer — HTTP and WS crypto payloads."""

import base64
from typing import Literal

from pydantic import BaseModel

from core.models.crypto import AESGCMCipherPayload

class CipherHTTPRequest(BaseModel):
    """HTTP request with encrypted payload (includes RSA-encrypted AES key)."""

    encrypted_key: str  # base64
    nonce: str          # base64
    cipher_data: str    # base64

    def to_raw_payload(self) -> tuple[AESGCMCipherPayload, bytes]:
        """Convert to raw dataclass payload + encrypted_key bytes."""
        return AESGCMCipherPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            nonce=base64.b64decode(self.nonce),
        ), base64.b64decode(self.encrypted_key)


class CipherHTTPResponse(BaseModel):
    """HTTP response with encrypted payload (AES-only, client already has key)."""

    nonce: str          # base64
    cipher_data: str    # base64

    def to_raw_payload(self) -> AESGCMCipherPayload:
        return AESGCMCipherPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            nonce=base64.b64decode(self.nonce),
        )

    @staticmethod
    def from_raw_payload(payload: AESGCMCipherPayload) -> "CipherHTTPResponse":
        return CipherHTTPResponse(
            nonce=base64.b64encode(payload.nonce).decode(),
            cipher_data=base64.b64encode(payload.cipher_data).decode(),
        )


class WSKeyExchangeRequest(BaseModel):
    """WS key exchange: client sends RSA-encrypted AES session key."""

    type: Literal["key_exchange"]
    encrypted_key: str  # base64

    def to_raw_key(self) -> bytes:
        return base64.b64decode(self.encrypted_key)


class WSCipherMessage(BaseModel):
    """WS encrypted message (after key exchange, both directions)."""

    type: Literal["encrypted"]
    nonce: str          # base64
    cipher_data: str    # base64

    def to_raw_payload(self) -> AESGCMCipherPayload:
        return AESGCMCipherPayload(
            cipher_data=base64.b64decode(self.cipher_data),
            nonce=base64.b64decode(self.nonce),
        )

    @staticmethod
    def from_raw_payload(payload: AESGCMCipherPayload) -> "WSCipherMessage":
        return WSCipherMessage(
            type="encrypted",
            nonce=base64.b64encode(payload.nonce).decode(),
            cipher_data=base64.b64encode(payload.cipher_data).decode(),
        )
