"""AES-256-GCM encryption for the secrets vault.

Master key:
  - 32 random bytes.
  - Loaded from MASTER_KEY_PATH if set; otherwise auto-generated at first
    startup and stored at /data/master_key.bin inside the data volume.
  - The user is responsible for backing up the data volume. Losing the key
    AND the encrypted ciphertexts makes secrets unrecoverable. That's by
    design — the key+DB pair is the trust anchor.

Ciphertext format (base64-encoded):
  12-byte nonce || ciphertext || 16-byte GCM tag
"""

import base64
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from server.config import config

_DEFAULT_KEY_PATH = Path("/data/master_key.bin")


def _load_or_create_master_key() -> bytes:
    # Honor explicit path first
    if config.MASTER_KEY_PATH:
        p = Path(config.MASTER_KEY_PATH)
        if p.exists():
            data = p.read_bytes()
            if len(data) == 32:
                return data
            # base64-encoded?
            try:
                decoded = base64.b64decode(data)
                if len(decoded) == 32:
                    return decoded
            except Exception:
                pass
        # If the configured path exists but is wrong, fail loud
        if p.exists():
            raise RuntimeError(
                f"master key at {p} is not 32 bytes raw or base64-32 — refusing to start"
            )

    # Fall back to /data/master_key.bin
    p = _DEFAULT_KEY_PATH
    if p.exists():
        data = p.read_bytes()
        if len(data) == 32:
            return data
        raise RuntimeError(f"existing master key at {p} is not 32 bytes — refusing to overwrite")

    # Generate
    key = secrets.token_bytes(32)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(key)
    os.chmod(p, 0o600)
    return key


_MASTER_KEY: bytes | None = None


def _key() -> bytes:
    global _MASTER_KEY
    if _MASTER_KEY is None:
        _MASTER_KEY = _load_or_create_master_key()
    return _MASTER_KEY


def encrypt(plaintext: str) -> str:
    aesgcm = AESGCM(_key())
    nonce = secrets.token_bytes(12)
    ct = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return base64.b64encode(nonce + ct).decode("ascii")


def decrypt(blob_b64: str) -> str:
    raw = base64.b64decode(blob_b64.encode("ascii"))
    nonce, ct = raw[:12], raw[12:]
    aesgcm = AESGCM(_key())
    pt = aesgcm.decrypt(nonce, ct, None)
    return pt.decode("utf-8")
