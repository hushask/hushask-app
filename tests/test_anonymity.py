"""
tests/test_anonymity.py — Anonymity enforcement tests for HushAsk.

Verifies that user identity is never stored raw in the database,
and that source channels are properly purged after delivery.
"""
import os
import pytest
import sqlite3

# Set required env vars before importing app (which checks HASH_SALT at module level)
os.environ.setdefault("HASH_SALT", "test-salt-for-testing-anonymity-suite")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "test-client-id")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("NOTION_ENCRYPTION_KEY", "")

import database
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture
def hash_user_fn():
    """Import hash_user from app with env properly set."""
    # We import here to avoid triggering app startup side-effects at module level
    # while still getting the real hash_user function
    import importlib
    import sys
    # If app is already imported (from a previous test), use it; otherwise import fresh
    if "app" in sys.modules:
        from app import hash_user
    else:
        with patch("slack_bolt.App"), \
             patch("slack_bolt.oauth.oauth_settings.OAuthSettings"), \
             patch("database.init_db"):
            import app
            from app import hash_user
    return hash_user


# ── hash_user determinism and isolation ──────────────────────────────────────

def test_hash_user_is_deterministic(hash_user_fn):
    h1 = hash_user_fn("U_ABC", "T_TEAM1")
    h2 = hash_user_fn("U_ABC", "T_TEAM1")
    assert h1 == h2


def test_hash_user_differs_for_different_user_ids(hash_user_fn):
    h1 = hash_user_fn("U_USER1", "T_TEAM1")
    h2 = hash_user_fn("U_USER2", "T_TEAM1")
    assert h1 != h2


def test_hash_user_differs_for_different_team_ids(hash_user_fn):
    h1 = hash_user_fn("U_SAME", "T_TEAM1")
    h2 = hash_user_fn("U_SAME", "T_TEAM2")
    assert h1 != h2


def test_hash_user_output_is_64_hex_chars(hash_user_fn):
    h = hash_user_fn("U_ABC123", "T_XYZ")
    assert len(h) == 64, f"Expected 64 hex chars, got {len(h)}"
    assert all(c in "0123456789abcdef" for c in h), "Hash is not lowercase hex"


def test_hash_user_output_does_not_contain_raw_user_id(hash_user_fn):
    user_id = "U_SECRETUSERID"
    team_id = "T_TEAM1"
    h = hash_user_fn(user_id, team_id)
    assert user_id not in h, "Raw user_id found in hash output!"


# ── log_delivered anonymity ───────────────────────────────────────────────────

def test_log_delivered_row_does_not_contain_raw_user_id():
    raw_user_id = "U_RAWUSERID999"
    user_hash = "deadbeef" * 8  # fake 64-char hash

    msg_id = database.log_delivered(
        team_id="T_ANON",
        target_channel="C_TRIAGE",
        route_type="hr",
        message="Anonymous message content",
        user_hash=user_hash,
        source_channel="C_SOURCE",
        thread_ts="1234567890.000001",
    )

    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert row is not None

        # Check every column value for raw user_id
        for key in row.keys():
            val = row[key]
            if isinstance(val, str):
                assert raw_user_id not in val, (
                    f"Raw user_id found in column '{key}': {val}"
                )


# ── mark_replied_and_purge_source ─────────────────────────────────────────────

def _setup_routing_and_delivery(team_id, thread_ts, user_hash, source_channel):
    """Helper: insert routing + delivered_messages entries, return msg_id."""
    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE",
        route_type="hr",
        message="Test message",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )
    return msg_id


def test_mark_replied_and_purge_source_nulls_routing_table():
    team_id, thread_ts = "T_PURGE1", "ts_111.001"
    msg_id = _setup_routing_and_delivery(team_id, thread_ts, "hx01", "C_SRC_111")

    database.mark_replied_and_purge_source(msg_id)

    routing = database.get_routing(team_id, thread_ts)
    assert routing is not None  # row still exists
    assert routing["source_channel"] is None, "source_channel should be NULL in routing_table"


def test_mark_replied_and_purge_source_nulls_delivered_messages():
    team_id, thread_ts = "T_PURGE2", "ts_222.001"
    msg_id = _setup_routing_and_delivery(team_id, thread_ts, "hx02", "C_SRC_222")

    database.mark_replied_and_purge_source(msg_id)

    # New behaviour: delivered_messages.source_channel is NOT NULLed by mark_replied_and_purge_source
    # It persists until purge_delivered_source_channel() is explicitly called at thread close
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert row is not None
        assert row["source_channel"] is not None, (
            "source_channel should still be set in delivered_messages after mark_replied_and_purge_source"
        )

    # Now explicitly call purge_delivered_source_channel — THEN it should be NULL
    database.purge_delivered_source_channel(team_id, thread_ts)

    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert row is not None
        assert row["source_channel"] is None, (
            "source_channel should be NULL in delivered_messages after purge_delivered_source_channel"
        )


# ── purge_source_channels safety sweep ───────────────────────────────────────

def test_purge_source_channels_nulls_source_for_replied_threads():
    team_id, thread_ts = "T_SWEEP1", "ts_333.001"
    user_hash = "hx03"
    source_channel = "C_SRC_333"

    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE",
        route_type="hr",
        message="Sweep test message",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # Mark as replied without purging (simulates a message that slipped through)
    database.mark_replied(msg_id)

    # Run the safety sweep
    database.purge_source_channels()

    # source_channel in routing_table should now be NULL
    routing = database.get_routing(team_id, thread_ts)
    assert routing is not None
    assert routing["source_channel"] is None, (
        "purge_source_channels() should NULL source_channel for replied threads"
    )
