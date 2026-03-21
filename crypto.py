"""
crypto.py — Fernet symmetric encryption for HushAsk credential storage.

Key management:
  - NOTION_ENCRYPTION_KEY env var must be a URL-safe base64-encoded 32-byte key.
  - Generate with: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
  - If key is not set, encryption is DISABLED and tokens are stored/read as plaintext.
    This maintains backwards compatibility for local dev but logs a prominent warning.
"""

import os
import logging

logger = logging.getLogger(__name__)

_fernet = None
_encryption_enabled = False

def _init():
    global _fernet, _encryption_enabled
    key = os.environ.get("NOTION_ENCRYPTION_KEY", "")
    if not key:
        logger.warning(
            "[crypto] NOTION_ENCRYPTION_KEY not set — Notion tokens stored in plaintext. "
            "Set this env var before production use."
        )
        return
    try:
        from cryptography.fernet import Fernet
        _fernet = Fernet(key.encode() if isinstance(key, str) else key)
        _encryption_enabled = True
        logger.info("[crypto] Notion token encryption enabled.")
    except Exception as e:
        logger.error(f"[crypto] Failed to initialize encryption: {e} — falling back to plaintext.")

_init()


def encrypt_token(plaintext: str | None) -> str | None:
    """Encrypt a token string. Returns plaintext unchanged if encryption not enabled."""
    if not plaintext:
        return plaintext
    if not _encryption_enabled or _fernet is None:
        return plaintext
    try:
        return _fernet.encrypt(plaintext.encode()).decode()
    except Exception as e:
        logger.error(f"[crypto] Encryption failed: {e} — storing plaintext.")
        return plaintext


def decrypt_token(stored: str | None) -> str | None:
    """Decrypt a stored token. If decryption fails (e.g. was stored as plaintext),
    returns the value as-is (migration path for existing plaintext tokens)."""
    if not stored:
        return stored
    if not _encryption_enabled or _fernet is None:
        return stored
    try:
        return _fernet.decrypt(stored.encode()).decode()
    except Exception:
        # Decryption failed — treat as plaintext (migration path for existing tokens)
        logger.warning("[crypto] Token decryption failed — treating as plaintext (migration path).")
        return stored
