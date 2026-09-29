"""RSA + AES-256-GCM hybrid encryption.

Server generates and persists an RSA key pair. Clients fetch the public key
via /api/crypto/public-key, generate a random AES-256 session key, and encrypt
it with RSA-OAEP. All subsequent messages use AES-256-GCM with the shared
session key (fresh IV per message).
"""

import logging
import os
from pathlib import Path
from typing import cast

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey, RSAPublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from typing_extensions import override

from core.crypto.base import CryptoBase
from core.crypto.constants import GCM_NONCE_SIZE
from core.models.crypto import (
    AESGCMCipherPayload,
    AESGCMPlainPayload,
    RSAAESCipherPayload,
    RSAAESPlainPayload,
)

logger = logging.getLogger(__name__)


class AESGCMSession(CryptoBase[AESGCMCipherPayload, AESGCMPlainPayload]):
    """AES-256-GCM session with a shared symmetric key."""

    def __init__(self, session_key: bytes) -> None:
        self._session_key: bytes = session_key

    @override
    def encrypt(self, payload: AESGCMPlainPayload) -> AESGCMCipherPayload:
        # A fresh 96-bit random nonce per message is mandatory: reusing a (key, nonce)
        # pair breaks AES-GCM confidentiality and authentication.
        nonce = os.urandom(GCM_NONCE_SIZE)
        aesgcm = AESGCM(self._session_key)
        cipher_data = aesgcm.encrypt(nonce, payload.plain_data, None)
        return AESGCMCipherPayload(
            cipher_data=cipher_data,
            nonce=nonce,
        )

    @override
    def decrypt(self, payload: AESGCMCipherPayload) -> AESGCMPlainPayload:
        aesgcm = AESGCM(self._session_key)
        plaintext = aesgcm.decrypt(payload.nonce, payload.cipher_data, None)
        return AESGCMPlainPayload(plain_data=plaintext)


class RSAAESCrypto(CryptoBase[RSAAESCipherPayload, RSAAESPlainPayload]):
    """RSA-OAEP key exchange + AES-256-GCM data encryption.

    Keys are loaded from key_dir if present, otherwise generated (and persisted when key_dir is set).
    """

    PADDING: padding.OAEP = padding.OAEP(
        mgf=padding.MGF1(algorithm=hashes.SHA256()),
        algorithm=hashes.SHA256(),
        label=None,
    )

    def __init__(
        self,
        key_dir: Path | None = None,
        key_size: int = 2048,
    ) -> None:
        self._key_dir: Path | None = key_dir
        self._key_size: int = key_size

        private_key, public_key = self._load_or_generate_keys(key_dir, key_size)
        self._private_key: RSAPrivateKey = private_key
        self._public_key: RSAPublicKey = public_key

    @staticmethod
    def _load_or_generate_keys(
        key_dir: Path | None, key_size: int
    ) -> tuple[RSAPrivateKey, RSAPublicKey]:
        if key_dir is not None:
            private_path = key_dir / "private_key.pem"
            public_path = key_dir / "public_key.pem"

            if private_path.exists() and public_path.exists():
                logger.info("Loading RSA key pair from %s", key_dir)
                private_key: RSAPrivateKey = cast(
                    RSAPrivateKey,
                    serialization.load_pem_private_key(private_path.read_bytes(), password=None),
                )
                public_key: RSAPublicKey = cast(
                    RSAPublicKey, serialization.load_pem_public_key(public_path.read_bytes())
                )
                return private_key, public_key

        logger.info("Generating RSA-%d key pair", key_size)
        private_key = rsa.generate_private_key(
            public_exponent=65537,
            key_size=key_size,
        )
        public_key = private_key.public_key()

        if key_dir is not None:
            key_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            private_path = key_dir / "private_key.pem"
            public_path = key_dir / "public_key.pem"

            private_path.write_bytes(
                private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption(),
                )
            )
            os.chmod(private_path, 0o600)

            public_path.write_bytes(
                public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo,
                )
            )
            logger.info("RSA key pair saved to %s", key_dir)

        return private_key, public_key

    @property
    def public_key_pem(self) -> str:
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode()

    def create_session(self, encrypted_key: bytes) -> AESGCMSession:
        """RSA-decrypt the encrypted key and create an AES-GCM session."""
        session_key = self._private_key.decrypt(encrypted_key, self.PADDING)
        return AESGCMSession(session_key)

    @override
    def encrypt(self, payload: RSAAESPlainPayload) -> RSAAESCipherPayload:
        """RSA-decrypt the AES key, then AES-GCM encrypt the payload."""
        session = self.create_session(payload.encrypted_key)
        cipher_payload = session.encrypt(AESGCMPlainPayload(plain_data=payload.plain_data))
        return RSAAESCipherPayload(
            encrypted_key=payload.encrypted_key,
            cipher_data=cipher_payload.cipher_data,
            nonce=cipher_payload.nonce,
        )

    @override
    def decrypt(self, payload: RSAAESCipherPayload) -> RSAAESPlainPayload:
        """RSA-decrypt the AES key, then AES-GCM decrypt the payload."""
        session = self.create_session(payload.encrypted_key)
        plain_payload = session.decrypt(
            AESGCMCipherPayload(cipher_data=payload.cipher_data, nonce=payload.nonce)
        )
        return RSAAESPlainPayload(
            encrypted_key=payload.encrypted_key,
            plain_data=plain_payload.plain_data,
        )
