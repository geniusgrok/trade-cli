"""Recipient-sealed transport for the approved independent paper seed only."""
import base64
import hashlib
import re

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
import os

IDENTITY = 'a92-inherited-v1'
OLD_SOURCE = 'd2fee61a7f91679f6fdabaf97ba68cd030ad290a'
SOURCE = 'a92d79dad4fc21d38aab26a36f31f58a34b2dd62'
DOMAIN = b'trade-cli approved checkpoint import v1'


def import_aad(payload_sha256: str) -> bytes:
    if not re.fullmatch('[0-9a-f]{64}', payload_sha256):
        raise ValueError('IMPORT_PAYLOAD_HASH')
    return b'|'.join([DOMAIN, IDENTITY.encode(), OLD_SOURCE.encode(), SOURCE.encode(),
                      b'2026-09-30', payload_sha256.encode()])


def _receiver(state_key: bytes) -> X25519PrivateKey:
    if len(state_key) != 32:
        raise ValueError('IMPORT_KEY_LENGTH')
    material = HKDF(algorithm=hashes.SHA256(), length=32,
                    salt=hashlib.sha256(DOMAIN + b'|receiver').digest(),
                    info=(IDENTITY + '|' + SOURCE).encode()).derive(state_key)
    return X25519PrivateKey.from_private_bytes(material)


def import_public_key(state_key: bytes) -> str:
    return base64.b64encode(_receiver(state_key).public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def _session_key(shared: bytes, sender: bytes, recipient: bytes) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=32,
                salt=hashlib.sha256(DOMAIN + sender + recipient).digest(),
                info=b'independent paper seed AES-256-GCM').derive(shared)


def seal_import(payload: bytes, public_key: str) -> bytes:
    """Confidentiality only: the receiver also requires a trusted cipher/payload SHA."""
    recipient = base64.b64decode(public_key, validate=True)
    if len(recipient) != 32 or len(payload) > 8 * 1024 * 1024:
        raise ValueError('IMPORT_SIZE_OR_RECIPIENT')
    ephemeral = X25519PrivateKey.generate()
    sender = ephemeral.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _session_key(ephemeral.exchange(X25519PublicKey.from_public_bytes(recipient)), sender, recipient)
    nonce = os.urandom(12)
    return b'TAI1' + sender + nonce + AESGCM(key).encrypt(nonce, payload, import_aad(hashlib.sha256(payload).hexdigest()))


def open_import(sealed: bytes, state_key: bytes, payload_sha256: str) -> bytes:
    if not sealed.startswith(b'TAI1') or not 64 <= len(sealed) <= 8 * 1024 * 1024 + 64:
        raise ValueError('IMPORT_ENVELOPE')
    receiver = _receiver(state_key)
    recipient = receiver.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key = _session_key(receiver.exchange(X25519PublicKey.from_public_bytes(sealed[4:36])), sealed[4:36], recipient)
    payload = AESGCM(key).decrypt(sealed[36:48], sealed[48:], import_aad(payload_sha256))
    if hashlib.sha256(payload).hexdigest() != payload_sha256:
        raise ValueError('IMPORT_PAYLOAD_HASH')
    return payload
