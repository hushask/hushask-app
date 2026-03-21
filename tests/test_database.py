"""
tests/test_database.py — Core DB function tests for HushAsk.

All tests use a per-test temp SQLite file (never touches /data/hushask.db).
"""
import os
import sqlite3
import pytest
from datetime import datetime, timezone, timedelta

# Must set env vars before importing database (which imports crypto)
os.environ.setdefault("NOTION_ENCRYPTION_KEY", "")

import database


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    database.init_db()
    yield
    database.DB_PATH = original


# ── init_db ───────────────────────────────────────────────────────────────────

def test_init_db_creates_all_tables(tmp_path):
    db_file = str(tmp_path / "fresh.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    try:
        database.init_db()
        conn = sqlite3.connect(db_file)
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        expected = {
            "workspaces", "workspace_config", "workspace_usage",
            "notion_auth_states", "slack_oauth_states", "pending_messages",
            "delivered_messages", "install_nudges", "routing_table", "message_mappings",
        }
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"
        conn.close()
    finally:
        database.DB_PATH = original


# ── Workspace / bot token round-trip ─────────────────────────────────────────

def test_save_and_find_bot_token():
    database.save_workspace(
        team_id="T001",
        enterprise_id="",
        team_name="Test Workspace",
        bot_token="xoxb-test-token-123",
        bot_user_id="U_BOT",
        app_id="A001",
        installer_user_id="U_INST",
    )
    token = database.find_bot_token(None, "T001")
    assert token == "xoxb-test-token-123"


def test_find_bot_token_missing_returns_none():
    result = database.find_bot_token(None, "T_NONEXISTENT")
    assert result is None


def test_save_workspace_upsert():
    database.save_workspace("T002", "", "Old Name", "xoxb-old", "UBOT")
    database.save_workspace("T002", "", "New Name", "xoxb-new", "UBOT")
    token = database.find_bot_token(None, "T002")
    assert token == "xoxb-new"


# ── Workspace config round-trip ───────────────────────────────────────────────

def test_save_and_get_workspace_config():
    database.save_workspace_config(
        workspace_id="T003",
        installer_id="U003",
        public_channel="C_PUBLIC",
        hr_channel="C_HR",
        notion_api_key=None,
        notion_database_id=None,
    )
    config = database.get_workspace_config("T003")
    assert config is not None
    assert config["public_channel"] == "C_PUBLIC"
    assert config["hr_channel"] == "C_HR"
    assert config["installer_id"] == "U003"


def test_get_workspace_config_missing_returns_none():
    result = database.get_workspace_config("T_GHOST")
    assert result is None


def test_workspace_config_upsert_updates_channels():
    database.save_workspace_config("T004", "U004", "C_PUB1", "C_HR1")
    database.save_workspace_config("T004", "U004", "C_PUB2", "C_HR2")
    config = database.get_workspace_config("T004")
    assert config["public_channel"] == "C_PUB2"
    assert config["hr_channel"] == "C_HR2"


# ── Freemium usage cap ────────────────────────────────────────────────────────

def test_check_and_increment_allows_up_to_free_limit():
    original_limit = database.FREE_LIMIT
    database.FREE_LIMIT = 3
    try:
        workspace = "T_FREE_01"
        for i in range(3):
            allowed, count = database.check_and_increment(workspace)
            assert allowed is True, f"Should be allowed on call {i+1}"
        # 4th call should be blocked
        allowed, count = database.check_and_increment(workspace)
        assert allowed is False
        assert count == 3
    finally:
        database.FREE_LIMIT = original_limit


def test_check_and_increment_blocks_at_limit():
    original_limit = database.FREE_LIMIT
    database.FREE_LIMIT = 1
    try:
        workspace = "T_FREE_02"
        allowed, _ = database.check_and_increment(workspace)
        assert allowed is True
        allowed, count = database.check_and_increment(workspace)
        assert allowed is False
        assert count == 1
    finally:
        database.FREE_LIMIT = original_limit


def test_check_and_increment_bypasses_for_pro():
    # Make workspace Pro
    database.save_workspace("T_PRO_01", "", "Pro WS", "xoxb-pro", "UBOT")
    database.upgrade_to_pro("T_PRO_01")
    original_limit = database.FREE_LIMIT
    database.FREE_LIMIT = 0  # Would block everyone on free
    try:
        for _ in range(5):
            allowed, count = database.check_and_increment("T_PRO_01")
            assert allowed is True
            assert count == 0  # Pro returns (True, 0)
    finally:
        database.FREE_LIMIT = original_limit


def test_check_and_increment_resets_after_30_days():
    original_limit = database.FREE_LIMIT
    database.FREE_LIMIT = 2
    try:
        workspace = "T_RESET_01"
        # Fill up the quota
        database.check_and_increment(workspace)
        database.check_and_increment(workspace)
        allowed, _ = database.check_and_increment(workspace)
        assert allowed is False

        # Backdate the reset timestamp to 31 days ago
        old_ts = (datetime.now(timezone.utc) - timedelta(days=31)).isoformat()
        with database.get_conn() as conn:
            conn.execute(
                "UPDATE workspace_usage SET count_reset_at = ? WHERE workspace_id = ?",
                (old_ts, workspace)
            )

        # Should be allowed again after reset
        allowed, count = database.check_and_increment(workspace)
        assert allowed is True
        assert count == 1
    finally:
        database.FREE_LIMIT = original_limit


# ── Pending messages ──────────────────────────────────────────────────────────

def test_save_and_claim_pending_round_trip():
    token = "tok_abc123"
    database.save_pending(
        token=token,
        team_id="T_PEND_01",
        source_channel="C_SRC",
        message="Hello anonymous world",
        user_hash="hash_xyz",
        message_ts="1234567890.000001",
    )
    result = database.claim_pending(token)
    assert result is not None
    assert result["token"] == token
    assert result["message"] == "Hello anonymous world"
    assert result["user_hash"] == "hash_xyz"
    assert result["team_id"] == "T_PEND_01"


def test_claim_pending_returns_none_on_second_call():
    token = "tok_once_only"
    database.save_pending(
        token=token,
        team_id="T_PEND_02",
        source_channel="C_SRC",
        message="Atomic message",
        user_hash="hash_abc",
    )
    first = database.claim_pending(token)
    second = database.claim_pending(token)
    assert first is not None
    assert second is None


# ── close_thread idempotency ──────────────────────────────────────────────────

def test_close_thread_true_first_false_second():
    database.save_routing("T_RT_01", "ts_1234.0001", "hash_user1", "C_SOURCE")
    first = database.close_thread("T_RT_01", "ts_1234.0001")
    second = database.close_thread("T_RT_01", "ts_1234.0001")
    assert first is True
    assert second is False


def test_close_thread_nonexistent_returns_false():
    result = database.close_thread("T_GHOST", "ts_ghost.0001")
    assert result is False


# ── purge_expired_routing ─────────────────────────────────────────────────────

def test_purge_expired_routing_deletes_old_keeps_recent():
    old_ts = (datetime.now(timezone.utc) - timedelta(days=35)).strftime("%Y-%m-%d %H:%M")
    new_ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")

    with database.get_conn() as conn:
        conn.execute(
            "INSERT INTO routing_table (team_id, thread_ts, user_hash, source_channel, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("T_PURGE", "ts_old.001", "hash_old", "C_SRC", old_ts)
        )
        conn.execute(
            "INSERT INTO routing_table (team_id, thread_ts, user_hash, source_channel, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("T_PURGE", "ts_new.001", "hash_new", "C_SRC", new_ts)
        )

    deleted = database.purge_expired_routing(days=30)
    assert deleted == 1

    # New entry should still be present
    remaining = database.get_routing("T_PURGE", "ts_new.001")
    assert remaining is not None

    # Old entry should be gone
    gone = database.get_routing("T_PURGE", "ts_old.001")
    assert gone is None


# ── message_mappings round-trip ───────────────────────────────────────────────

def test_save_and_get_message_mapping():
    database.save_message_mapping(
        team_id="T_MAP_01",
        user_dm_ts="1111111111.000001",
        triage_thread_ts="2222222222.000001",
        triage_message_ts="2222222222.000002",
        triage_channel="C_TRIAGE",
    )
    result = database.get_message_mapping("T_MAP_01", "1111111111.000001")
    assert result is not None
    assert result["triage_thread_ts"] == "2222222222.000001"
    assert result["triage_channel"] == "C_TRIAGE"


def test_get_message_mapping_missing_returns_none():
    result = database.get_message_mapping("T_MAP_01", "9999999999.000001")
    assert result is None
