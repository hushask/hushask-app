"""
tests/test_twoway_chat.py — Two-way chat lifecycle tests for HushAsk.

Tests the full reply/fallback/close cycle including:
- Second reply fallback to delivered_messages when routing_table source is purged
- purge_delivered_source_channel at thread close
- Full source_channel preservation through reply cycle
- close_thread removes routing row
- purge_source_channels (startup sweep) behaviour for open vs closed threads
"""
import os
import pytest

os.environ.setdefault("NOTION_ENCRYPTION_KEY", "")
os.environ.setdefault("HASH_SALT", "test-salt-for-testing")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "test-client-id")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")

import database


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    database.init_db()
    yield
    database.DB_PATH = original


# ── 1: Second reply falls back to delivered_messages ─────────────────────────

def test_second_reply_uses_delivered_messages_fallback():
    team_id = "T_2WAY01"
    thread_ts = "ts_2way_001.001"
    source_channel = "C_DM_SOURCE_01"
    user_hash = "hash_2way01"
    target_channel = "C_TRIAGE_01"

    # Setup: routing + delivered_messages entry
    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel=target_channel,
        route_type="hr",
        message="First anonymous message",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # First reply: mark_replied_and_purge_source NULLs routing_table.source_channel
    database.mark_replied_and_purge_source(msg_id)

    # routing_table.source_channel should now be NULL
    routing = database.get_routing(team_id, thread_ts)
    assert routing is not None
    assert routing["source_channel"] is None, (
        "routing_table.source_channel should be NULL after first reply"
    )

    # delivered_messages.source_channel should still be set
    delivered = database.get_delivered_by_thread(team_id, thread_ts)
    assert delivered is not None
    assert delivered["source_channel"] is not None, (
        "delivered_messages.source_channel should still be set after first reply"
    )

    # Simulate second reply lookup: routing has NULL source, fall back to delivered_messages
    fallback_source = database.get_delivered_by_thread(team_id, thread_ts)
    assert fallback_source is not None
    assert fallback_source["source_channel"] == source_channel, (
        f"Fallback source_channel should be '{source_channel}'"
    )


# ── 2: purge_delivered_source_channel at close ───────────────────────────────

def test_purge_delivered_source_channel_at_close():
    team_id = "T_2WAY02"
    thread_ts = "ts_2way_002.001"
    source_channel = "C_DM_SOURCE_02"

    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE_02",
        route_type="hr",
        message="Message to close",
        user_hash="hash_2way02",
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # Confirm source_channel is set
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
    assert row["source_channel"] == source_channel

    # Call purge_delivered_source_channel
    database.purge_delivered_source_channel(team_id, thread_ts)

    # Should now be NULL
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
    assert row is not None
    assert row["source_channel"] is None, (
        "delivered_messages.source_channel should be NULL after purge_delivered_source_channel"
    )


# ── 3: source_channel preserved through full reply cycle ─────────────────────

def test_source_channel_preserved_through_reply_cycle():
    team_id = "T_2WAY03"
    thread_ts = "ts_2way_003.001"
    source_channel = "C_DM_SOURCE_03"
    user_hash = "hash_2way03"

    # Full cycle setup
    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE_03",
        route_type="hr",
        message="Full cycle test",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # After mark_replied_and_purge_source:
    # routing_table.source_channel → NULL
    # delivered_messages.source_channel → still set
    database.mark_replied_and_purge_source(msg_id)

    routing = database.get_routing(team_id, thread_ts)
    assert routing["source_channel"] is None, "routing_table.source_channel should be NULL"

    delivered = database.get_delivered_by_thread(team_id, thread_ts)
    assert delivered["source_channel"] is not None, (
        "delivered_messages.source_channel should still be set"
    )
    assert delivered["source_channel"] == source_channel

    # After purge_delivered_source_channel:
    # delivered_messages.source_channel → NULL
    database.purge_delivered_source_channel(team_id, thread_ts)

    delivered_after = database.get_delivered_by_thread(team_id, thread_ts)
    assert delivered_after["source_channel"] is None, (
        "delivered_messages.source_channel should be NULL after purge_delivered_source_channel"
    )


# ── 4: close_thread removes routing row ──────────────────────────────────────

def test_close_thread_removes_routing_row():
    team_id = "T_2WAY04"
    thread_ts = "ts_2way_004.001"

    database.save_routing(team_id, thread_ts, "hash_2way04", "C_DM_SOURCE_04")

    # Confirm row exists
    assert database.get_routing(team_id, thread_ts) is not None

    # Close the thread
    result = database.close_thread(team_id, thread_ts)
    assert result is True

    # Row should be gone
    assert database.get_routing(team_id, thread_ts) is None, (
        "get_routing should return None after close_thread"
    )


# ── 5: purge_source_channels preserves open (not-replied) threads ─────────────

def test_startup_sweep_preserves_open_threads():
    team_id = "T_2WAY05"
    thread_ts = "ts_2way_005.001"
    source_channel = "C_DM_SOURCE_05"
    user_hash = "hash_2way05"

    # Open thread: save_routing + log_delivered, but NO mark_replied call
    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE_05",
        route_type="hr",
        message="Open thread message",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # Run startup sweep
    database.purge_source_channels()

    # Open thread: delivered_messages.source_channel should still be set
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
    assert row is not None
    assert row["source_channel"] == source_channel, (
        "purge_source_channels should NOT purge delivered_messages.source_channel for open (unreplied) threads"
    )


# ── 6: purge_source_channels NULLs source for closed threads ─────────────────

def test_startup_sweep_nulls_closed_thread_source():
    team_id = "T_2WAY06"
    thread_ts = "ts_2way_006.001"
    source_channel = "C_DM_SOURCE_06"
    user_hash = "hash_2way06"

    # Setup: routing + delivered_messages
    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE_06",
        route_type="hr",
        message="Closed thread message",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # Close the thread — this removes the routing_table row
    database.close_thread(team_id, thread_ts)

    # Confirm routing row is gone, but delivered_messages still has source_channel
    assert database.get_routing(team_id, thread_ts) is None
    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
    assert row["source_channel"] == source_channel, (
        "source_channel should still be set before sweep"
    )

    # Run startup sweep: closed thread (no routing row) should get source_channel NULLed
    database.purge_source_channels()

    with database.get_conn() as conn:
        row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
    assert row is not None
    assert row["source_channel"] is None, (
        "purge_source_channels should NULL delivered_messages.source_channel for closed threads"
    )
