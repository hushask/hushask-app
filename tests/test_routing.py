"""
tests/test_routing.py — Routing flow tests for HushAsk.

Tests save_routing, get_routing, close_thread, mark_replied_and_purge_source,
and claim_pending atomicity under concurrent access.
"""
import os
import pytest
import threading
import sqlite3
from datetime import datetime, timezone, timedelta

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


# ── save_routing / get_routing round-trip ────────────────────────────────────

def test_save_routing_and_get_routing_round_trip():
    database.save_routing("T_R01", "ts_1000.001", "user_hash_abc", "C_SOURCE_01")
    result = database.get_routing("T_R01", "ts_1000.001")
    assert result is not None
    assert result["team_id"] == "T_R01"
    assert result["thread_ts"] == "ts_1000.001"
    assert result["user_hash"] == "user_hash_abc"
    assert result["source_channel"] == "C_SOURCE_01"


def test_get_routing_returns_none_for_missing():
    result = database.get_routing("T_GHOST", "ts_ghost.001")
    assert result is None


def test_get_routing_returns_none_after_close_thread():
    database.save_routing("T_R02", "ts_2000.001", "user_hash_xyz", "C_SOURCE_02")
    database.close_thread("T_R02", "ts_2000.001")
    result = database.get_routing("T_R02", "ts_2000.001")
    assert result is None


# ── close_thread idempotency ──────────────────────────────────────────────────

def test_close_thread_idempotency_second_call_returns_false():
    database.save_routing("T_R03", "ts_3000.001", "hash_idem", "C_SOURCE_03")
    first = database.close_thread("T_R03", "ts_3000.001")
    second = database.close_thread("T_R03", "ts_3000.001")
    assert first is True
    assert second is False


# ── claim_pending atomicity under concurrency ─────────────────────────────────

def test_claim_pending_atomic_concurrent_calls():
    """Under concurrent access, exactly one thread should claim the pending message."""
    token = "tok_concurrent_test"
    database.save_pending(
        token=token,
        team_id="T_CONC",
        source_channel="C_SRC",
        message="Concurrent test message",
        user_hash="hash_conc",
    )

    results = []
    errors = []

    def try_claim():
        try:
            row = database.claim_pending(token)
            results.append(row)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=try_claim) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"Unexpected errors in threads: {errors}"

    # Exactly one thread should have claimed the row
    successes = [r for r in results if r is not None]
    nones = [r for r in results if r is None]
    assert len(successes) == 1, f"Expected 1 claim, got {len(successes)}"
    assert len(nones) == 9, f"Expected 9 None returns, got {len(nones)}"
    assert successes[0]["token"] == token


# ── mark_replied_and_purge_source ─────────────────────────────────────────────

def test_mark_replied_and_purge_source_purges_both_tables():
    team_id = "T_MRP01"
    thread_ts = "ts_5000.001"
    source_channel = "C_SOURCE_MRP"
    user_hash = "hash_mrp01"

    database.save_routing(team_id, thread_ts, user_hash, source_channel)
    msg_id = database.log_delivered(
        team_id=team_id,
        target_channel="C_TRIAGE",
        route_type="hr",
        message="Purge test",
        user_hash=user_hash,
        source_channel=source_channel,
        thread_ts=thread_ts,
    )

    # Verify source_channel is set before purge
    routing_before = database.get_routing(team_id, thread_ts)
    assert routing_before["source_channel"] == source_channel

    database.mark_replied_and_purge_source(msg_id)

    # New behaviour: routing_table.source_channel is NULLed, but delivered_messages.source_channel is NOT
    routing_after = database.get_routing(team_id, thread_ts)
    assert routing_after["source_channel"] is None, "routing_table.source_channel should be NULL"

    with database.get_conn() as conn:
        dm_row = conn.execute(
            "SELECT source_channel, replied FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert dm_row["source_channel"] is not None, (
            "delivered_messages.source_channel should NOT be NULL yet — "
            "it persists until purge_delivered_source_channel() is called at thread close"
        )
        assert dm_row["replied"] == 1, "delivered_messages.replied should be 1"

    # Now call purge_delivered_source_channel — THEN it should be NULL
    database.purge_delivered_source_channel(team_id, thread_ts)

    with database.get_conn() as conn:
        dm_row = conn.execute(
            "SELECT source_channel FROM delivered_messages WHERE id = ?", (msg_id,)
        ).fetchone()
        assert dm_row["source_channel"] is None, (
            "delivered_messages.source_channel should be NULL after purge_delivered_source_channel"
        )


def test_mark_replied_and_purge_source_with_none_msg_id():
    """Should not raise even with msg_id=None."""
    # This is an edge case the real code handles gracefully
    database.mark_replied_and_purge_source(None)  # Should not raise
