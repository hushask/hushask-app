"""
tests/test_crypto.py — Encryption/decryption tests for HushAsk.

Tests Fernet encryption, fallback to plaintext, and the migration path
for tokens that were stored without encryption.
"""
import os
import pytest
import importlib


def _get_fresh_crypto(encryption_key=None):
    """Reload crypto module with a specific NOTION_ENCRYPTION_KEY setting."""
    import crypto

    # Set env before re-init
    if encryption_key is not None:
        os.environ["NOTION_ENCRYPTION_KEY"] = encryption_key
    else:
        os.environ.pop("NOTION_ENCRYPTION_KEY", None)

    # Re-run _init() to pick up the new env state
    crypto._fernet = None
    crypto._encryption_enabled = False
    crypto._init()
    return crypto


def _make_fernet_key():
    """Generate a fresh Fernet key for testing."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


# ── None passthrough ─────────────────────────────────────────────────────────

def test_encrypt_token_none_returns_none():
    import crypto
    assert crypto.encrypt_token(None) is None


def test_decrypt_token_none_returns_none():
    import crypto
    assert crypto.decrypt_token(None) is None


# ── Fallback to plaintext when key not set ────────────────────────────────────

def test_encrypt_falls_back_to_plaintext_when_no_key():
    c = _get_fresh_crypto(encryption_key="")
    result = c.encrypt_token("my-secret-token")
    assert result == "my-secret-token"


def test_decrypt_falls_back_to_plaintext_when_no_key():
    c = _get_fresh_crypto(encryption_key="")
    result = c.decrypt_token("my-stored-plaintext")
    assert result == "my-stored-plaintext"


# ── Encryption enabled (with key) ────────────────────────────────────────────

def test_encrypt_output_differs_from_input():
    key = _make_fernet_key()
    c = _get_fresh_crypto(encryption_key=key)
    plaintext = "xoxb-super-secret-notion-key"
    encrypted = c.encrypt_token(plaintext)
    assert encrypted != plaintext


def test_encrypt_decrypt_round_trip():
    key = _make_fernet_key()
    c = _get_fresh_crypto(encryption_key=key)
    original = "notion-api-key-secret-12345"
    encrypted = c.encrypt_token(original)
    decrypted = c.decrypt_token(encrypted)
    assert decrypted == original


def test_encrypt_decrypt_round_trip_with_env_key():
    """Verify round-trip when key is set via env var (the production path)."""
    key = _make_fernet_key()
    os.environ["NOTION_ENCRYPTION_KEY"] = key
    try:
        import crypto
        crypto._fernet = None
        crypto._encryption_enabled = False
        crypto._init()

        token = "production-style-notion-key-xyz"
        encrypted = crypto.encrypt_token(token)
        decrypted = crypto.decrypt_token(encrypted)
        assert decrypted == token
    finally:
        os.environ.pop("NOTION_ENCRYPTION_KEY", None)
        # Reset to no-encryption state
        import crypto
        crypto._fernet = None
        crypto._encryption_enabled = False
        crypto._init()


def test_decrypt_plaintext_returns_unchanged_migration_path():
    """decrypt_token on a plaintext value (not encrypted) should return it as-is."""
    key = _make_fernet_key()
    c = _get_fresh_crypto(encryption_key=key)
    plaintext_token = "old-plaintext-token-from-before-encryption"
    # When decryption fails, it falls back to returning the value unchanged
    result = c.decrypt_token(plaintext_token)
    assert result == plaintext_token


def test_encrypt_token_empty_string_passthrough():
    """Empty string is falsy — should pass through unchanged."""
    c = _get_fresh_crypto(encryption_key="")
    result = c.encrypt_token("")
    # Empty string is falsy in Python, so encrypt_token returns it as-is
    assert result == "" or result is None or result == ""


def test_encryption_enabled_flag():
    """Verify _encryption_enabled is True when key is set, False otherwise."""
    key = _make_fernet_key()
    c = _get_fresh_crypto(encryption_key=key)
    assert c._encryption_enabled is True

    c2 = _get_fresh_crypto(encryption_key="")
    assert c2._encryption_enabled is False


@pytest.fixture(autouse=True)
def reset_crypto_after_test():
    """Ensure crypto module is reset to a clean state after each test."""
    yield
    # After each test, reset crypto to no-key state to avoid test pollution
    import crypto
    os.environ.pop("NOTION_ENCRYPTION_KEY", None)
    crypto._fernet = None
    crypto._encryption_enabled = False
    crypto._init()
