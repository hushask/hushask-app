"""
database.py — HushAsk SQLite layer
"""

import sqlite3, os, secrets
from datetime import datetime, timezone

DB_PATH    = os.environ.get("DB_PATH", "hushask.db")
print(f"[db] DB_PATH={DB_PATH}", flush=True)
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "20"))


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")   # WAL + NORMAL is safe and fast
    conn.execute("PRAGMA busy_timeout=5000")    # wait up to 5s before "database locked"
    return conn


def _add_column_if_missing(conn, table: str, column: str, definition: str):
    """Idempotent ALTER TABLE — no-op if column already exists."""
    try:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        print(f"[db] Migration: added {table}.{column}")
    except Exception:
        pass  # Column already exists — sqlite raises OperationalError


def _migrate_routing_table_nullable_source():
    """Recreate routing_table without NOT NULL on source_channel (idempotent).

    Needed for Fix 2 (post-delivery source_channel purge). SQLite does not
    support ALTER COLUMN, so we do CREATE-copy-drop-rename under a transaction.
    """
    with get_conn() as conn:
        rows = conn.execute("PRAGMA table_info(routing_table)").fetchall()
        for row in rows:
            if row["name"] == "source_channel" and row["notnull"] == 1:
                conn.executescript("""
                    BEGIN;
                    CREATE TABLE IF NOT EXISTS routing_table_new (
                        id             INTEGER PRIMARY KEY AUTOINCREMENT,
                        team_id        TEXT NOT NULL,
                        thread_ts      TEXT NOT NULL,
                        user_hash      TEXT NOT NULL,
                        source_channel TEXT,
                        created_at     TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now')),
                        UNIQUE(team_id, thread_ts)
                    );
                    INSERT OR IGNORE INTO routing_table_new
                        (id, team_id, thread_ts, user_hash, source_channel, created_at)
                        SELECT id, team_id, thread_ts, user_hash, source_channel, created_at
                        FROM routing_table;
                    DROP TABLE routing_table;
                    ALTER TABLE routing_table_new RENAME TO routing_table;
                    COMMIT;
                """)
                print("[db] Migration: routing_table.source_channel is now nullable")
                break


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS workspaces (
                team_id         TEXT PRIMARY KEY,
                enterprise_id   TEXT DEFAULT '',
                team_name       TEXT,
                bot_token       TEXT NOT NULL,
                bot_user_id     TEXT,
                app_id          TEXT,
                installer_user_id TEXT,
                is_pro          INTEGER DEFAULT 0,
                installed_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS workspace_config (
                workspace_id       TEXT PRIMARY KEY,
                installer_id       TEXT,
                public_channel     TEXT,
                hr_channel         TEXT,
                notion_api_key     TEXT,
                notion_database_id TEXT,
                updated_at         TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS workspace_usage (
                workspace_id   TEXT PRIMARY KEY,
                message_count  INTEGER DEFAULT 0,
                count_reset_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS notion_auth_states (
                state      TEXT PRIMARY KEY,
                team_id    TEXT NOT NULL,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS slack_oauth_states (
                state      TEXT PRIMARY KEY,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS pending_messages (
                token          TEXT PRIMARY KEY,
                team_id        TEXT NOT NULL,
                source_channel TEXT NOT NULL,
                message        TEXT NOT NULL,
                user_hash      TEXT NOT NULL,
                message_ts     TEXT,
                created_at     TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now'))
            );

            CREATE TABLE IF NOT EXISTS delivered_messages (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id        TEXT NOT NULL,
                target_channel TEXT NOT NULL,
                route_type     TEXT NOT NULL,
                message        TEXT NOT NULL,
                user_hash      TEXT NOT NULL,
                notion_synced  INTEGER DEFAULT 0,
                source_channel TEXT,
                thread_ts      TEXT,
                replied        INTEGER DEFAULT 0,
                delivered_at   TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now'))
            );
            CREATE TABLE IF NOT EXISTS install_nudges (
                team_id    TEXT PRIMARY KEY,
                sent_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS routing_table (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id        TEXT NOT NULL,
                thread_ts      TEXT NOT NULL,
                user_hash      TEXT NOT NULL,
                source_channel TEXT,
                created_at     TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now')),
                UNIQUE(team_id, thread_ts)
            );

            CREATE TABLE IF NOT EXISTS message_mappings (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id           TEXT NOT NULL,
                user_dm_ts        TEXT NOT NULL,
                triage_thread_ts  TEXT NOT NULL,
                triage_message_ts TEXT NOT NULL,
                triage_channel    TEXT NOT NULL,
                created_at        TEXT DEFAULT (strftime('%Y-%m-%d %H:%M', 'now')),
                UNIQUE(team_id, user_dm_ts)
            );
        """)
    # Migrations for columns added after initial schema
    with get_conn() as conn:
        _add_column_if_missing(conn, "delivered_messages", "replied", "INTEGER DEFAULT 0")
        _add_column_if_missing(conn, "delivered_messages", "source_channel", "TEXT")
        _add_column_if_missing(conn, "delivered_messages", "thread_ts", "TEXT")
        _add_column_if_missing(conn, "pending_messages", "original_message_ts", "TEXT")
    # Migration: allow NULL source_channel in routing_table (Fix 2 — post-delivery purge)
    _migrate_routing_table_nullable_source()
    # Auto-purge expired Identity Vault entries on every startup
    purge_expired_routing()
    # Safety sweep: NULL out source_channel for already-replied routing entries
    purge_source_channels()
    print("[db] Initialized.")


# ── Identity Vault (routing_table) ───────────────────────────────────────────

def save_routing(team_id: str, thread_ts: str, user_hash: str, source_channel: str):
    """Store a thread_ts → user_hash mapping in the Identity Vault."""
    with get_conn() as conn:
        conn.execute("""
            INSERT OR IGNORE INTO routing_table
                (team_id, thread_ts, user_hash, source_channel, created_at)
            VALUES (?, ?, ?, ?, strftime('%Y-%m-%d %H:%M', 'now'))
        """, (team_id, thread_ts, user_hash, source_channel))


def get_routing(team_id: str, thread_ts: str):
    """Look up a routing record by team and thread_ts. Returns dict or None."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM routing_table WHERE team_id = ? AND thread_ts = ?",
            (team_id, thread_ts)
        ).fetchone()
        return dict(row) if row else None


def get_active_thread_for_user(team_id: str, user_hash: str):
    """Return the most recent routing_table entry for a user hash with a known triage thread.

    Used for 2-way anonymous chat: when a user replies to a delivery DM, we look up
    their original triage thread so we can post their follow-up anonymously.
    Returns a dict (with thread_ts) if found, else None.
    """
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT rt.thread_ts, dm.target_channel, dm.route_type
            FROM routing_table rt
            JOIN delivered_messages dm ON rt.team_id = dm.team_id AND rt.thread_ts = dm.thread_ts
            WHERE rt.team_id = ? AND rt.user_hash = ?
            ORDER BY rt.created_at DESC
            LIMIT 1
            """,
            (team_id, user_hash)
        ).fetchone()
        return dict(row) if row else None


def close_thread(team_id: str, thread_ts: str) -> None:
    """Remove the active thread record, preventing further 2-way routing."""
    with get_conn() as conn:
        conn.execute(
            "DELETE FROM routing_table WHERE team_id = ? AND thread_ts = ?",
            (team_id, thread_ts)
        )
        conn.commit()


def purge_expired_routing(days: int = 30) -> int:
    """Delete routing_table entries older than N days (default 30). Returns count deleted."""
    with get_conn() as conn:
        cur = conn.execute(
            f"DELETE FROM routing_table WHERE created_at < datetime('now', '-{days} days')"
        )
        deleted = cur.rowcount
        if deleted:
            print(f"[db] Identity Vault purge: removed {deleted} expired entries (>{days}d).")
        return deleted


# ── Workspace / Installation ──────────────────────────────────────────────────

def save_workspace(team_id: str, enterprise_id: str, team_name: str,
                   bot_token: str, bot_user_id: str = None,
                   app_id: str = None, installer_user_id: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO workspaces
                (team_id, enterprise_id, team_name, bot_token, bot_user_id, app_id, installer_user_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                enterprise_id     = excluded.enterprise_id,
                team_name         = excluded.team_name,
                bot_token         = excluded.bot_token,
                bot_user_id       = excluded.bot_user_id,
                app_id            = excluded.app_id,
                installer_user_id = excluded.installer_user_id
        """, (team_id, enterprise_id or '', team_name, bot_token,
              bot_user_id, app_id, installer_user_id))


def find_bot_token(enterprise_id: str | None, team_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT bot_token FROM workspaces WHERE team_id = ?", (team_id,)
        ).fetchone()
        return row["bot_token"] if row else None


def find_workspace_row(team_id: str) -> dict | None:
    """Return full workspace row (token, bot_user_id, app_id, etc.) for find_bot."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM workspaces WHERE team_id = ?", (team_id,)
        ).fetchone()
        return dict(row) if row else None


def find_installer_user_id(team_id: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT installer_user_id FROM workspaces WHERE team_id = ?", (team_id,)
        ).fetchone()
        return row["installer_user_id"] if row else None


def upgrade_to_pro(team_id: str):
    with get_conn() as conn:
        conn.execute("UPDATE workspaces SET is_pro = 1 WHERE team_id = ?", (team_id,))


def revoke_pro(team_id: str):
    """Downgrade workspace to free tier after subscription cancellation."""
    with get_conn() as conn:
        conn.execute("UPDATE workspaces SET is_pro = 0 WHERE team_id = ?", (team_id,))


def is_workspace_pro(team_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT is_pro FROM workspaces WHERE team_id = ?", (team_id,)).fetchone()
        return bool(row and row["is_pro"])


# ── Slack OAuth state store ───────────────────────────────────────────────────

def issue_slack_state() -> str:
    state = secrets.token_hex(16)
    with get_conn() as conn:
        conn.execute("DELETE FROM slack_oauth_states WHERE created_at < datetime('now', '-10 minutes')")
        conn.execute("INSERT INTO slack_oauth_states (state) VALUES (?)", (state,))
    return state


def consume_slack_state(state: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT state FROM slack_oauth_states WHERE state = ? AND created_at > datetime('now', '-10 minutes')",
            (state,)
        ).fetchone()
        if row:
            conn.execute("DELETE FROM slack_oauth_states WHERE state = ?", (state,))
            return True
        return False


# ── Workspace config ──────────────────────────────────────────────────────────

def get_workspace_config(workspace_id: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM workspace_config WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()
        return dict(row) if row else None


def save_workspace_config(workspace_id: str, installer_id: str,
                           public_channel: str, hr_channel: str,
                           notion_api_key: str = None, notion_database_id: str = None):
    with get_conn() as conn:
        # Atomically read + preserve existing Notion values within the same
        # connection so concurrent wizard3 retries can't race each other.
        existing = conn.execute(
            "SELECT notion_api_key, notion_database_id FROM workspace_config WHERE workspace_id = ?",
            (workspace_id,)
        ).fetchone()
        final_notion_key = notion_api_key or (existing["notion_api_key"] if existing else None)
        final_notion_db  = notion_database_id or (existing["notion_database_id"] if existing else None)

        conn.execute("""
            INSERT INTO workspace_config
                (workspace_id, installer_id, public_channel, hr_channel,
                 notion_api_key, notion_database_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(workspace_id) DO UPDATE SET
                -- Preserve installer_id if it's already set (first writer wins)
                installer_id       = COALESCE(installer_id, excluded.installer_id),
                public_channel     = excluded.public_channel,
                hr_channel         = excluded.hr_channel,
                notion_api_key     = excluded.notion_api_key,
                notion_database_id = excluded.notion_database_id,
                updated_at         = excluded.updated_at
        """, (workspace_id, installer_id, public_channel, hr_channel,
              final_notion_key, final_notion_db))

        print(f"[db] save_workspace_config: pub={public_channel} hr={hr_channel} "
              f"notion_key={bool(final_notion_key)} notion_db={bool(final_notion_db)}")


def save_workspace_notion(workspace_id: str, notion_api_key: str, notion_database_id: str):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO workspace_config (workspace_id, notion_api_key, notion_database_id, updated_at)
            VALUES (?, ?, ?, datetime('now'))
            ON CONFLICT(workspace_id) DO UPDATE SET
                notion_api_key     = excluded.notion_api_key,
                notion_database_id = excluded.notion_database_id,
                updated_at         = excluded.updated_at
        """, (workspace_id, notion_api_key, notion_database_id))


def reset_workspace_config(workspace_id: str):
    """Clear channel assignments + installer but PRESERVE Notion credentials.
    This prevents duplicate Hush Library DBs on every Reset → re-wizard cycle.
    If there's no row yet, the UPDATE is a no-op — safe."""
    with get_conn() as conn:
        conn.execute("""
            UPDATE workspace_config
            SET public_channel = NULL,
                hr_channel     = NULL,
                installer_id   = NULL,
                updated_at     = datetime('now')
            WHERE workspace_id = ?
        """, (workspace_id,))


# ── Notion OAuth states ───────────────────────────────────────────────────────

def store_notion_state(state: str, team_id: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM notion_auth_states WHERE created_at < datetime('now', '-1 hour')")
        conn.execute("INSERT OR REPLACE INTO notion_auth_states (state, team_id) VALUES (?, ?)", (state, team_id))


def get_team_from_state(state: str) -> str | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT team_id FROM notion_auth_states WHERE state = ? AND created_at > datetime('now', '-1 hour')",
            (state,)
        ).fetchone()
        return row["team_id"] if row else None


def delete_notion_state(state: str):
    with get_conn() as conn:
        conn.execute("DELETE FROM notion_auth_states WHERE state = ?", (state,))


# ── Freemium usage ────────────────────────────────────────────────────────────

def _ensure_usage(conn, workspace_id):
    conn.execute("INSERT OR IGNORE INTO workspace_usage (workspace_id) VALUES (?)", (workspace_id,))


def check_and_increment(workspace_id: str) -> tuple[bool, int]:
    """Check monthly cap (bypass for Pro). Returns (allowed, count)."""
    if is_workspace_pro(workspace_id):
        return True, 0

    now = datetime.now(timezone.utc).isoformat()
    with get_conn() as conn:
        _ensure_usage(conn, workspace_id)
        row = conn.execute(
            "SELECT message_count, count_reset_at FROM workspace_usage WHERE workspace_id = ?",
            (workspace_id,)
        ).fetchone()
        raw_ts = row["count_reset_at"].replace("Z", "+00:00")
        reset_at = datetime.fromisoformat(raw_ts)
        # Ensure both sides are timezone-aware before subtracting
        if reset_at.tzinfo is None:
            reset_at = reset_at.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - reset_at).days >= 30:
            conn.execute(
                "UPDATE workspace_usage SET message_count = 0, count_reset_at = ? WHERE workspace_id = ?",
                (now, workspace_id)
            )
            count = 0
        else:
            count = row["message_count"]

        if count >= FREE_LIMIT:
            return False, count
        conn.execute(
            "UPDATE workspace_usage SET message_count = message_count + 1 WHERE workspace_id = ?",
            (workspace_id,)
        )
        return True, count + 1


def get_usage(workspace_id: str) -> int:
    with get_conn() as conn:
        _ensure_usage(conn, workspace_id)
        row = conn.execute("SELECT message_count FROM workspace_usage WHERE workspace_id = ?", (workspace_id,)).fetchone()
        return row["message_count"] if row else 0


# ── Pending / Delivered messages ──────────────────────────────────────────────

def save_pending(token, team_id, source_channel, message, user_hash,
                 message_ts=None, original_message_ts=None):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending_messages
                (token, team_id, source_channel, message, user_hash, message_ts, original_message_ts, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M', 'now'))
        """, (token, team_id, source_channel, message, user_hash, message_ts, original_message_ts))


def get_pending(token):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM pending_messages WHERE token = ?", (token,)).fetchone()
        return dict(row) if row else None


def claim_pending(token):
    """Atomically DELETE and RETURN the pending_messages row for token.

    Uses DELETE ... RETURNING * so concurrent button clicks cannot both
    claim the same pending message (TOCTOU double-route race fix).
    Returns a dict if the row existed, None otherwise.
    """
    with get_conn() as conn:
        row = conn.execute(
            "DELETE FROM pending_messages WHERE token = ? RETURNING *",
            (token,)
        ).fetchone()
        return dict(row) if row else None


def peek_pending(token: str) -> dict | None:
    """Read a pending message by token without consuming it."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM pending_messages WHERE token = ?",
            (token,)
        ).fetchone()
        return dict(row) if row else None


def delete_pending(token):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_messages WHERE token = ?", (token,))


def log_delivered(team_id, target_channel, route_type, message, user_hash,
                  source_channel=None, thread_ts=None) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO delivered_messages
                (team_id, target_channel, route_type, message, user_hash, source_channel, thread_ts, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, strftime('%Y-%m-%d %H:%M', 'now'))
        """, (team_id, target_channel, route_type, message, user_hash, source_channel, thread_ts))
        return cur.lastrowid


def get_delivered(msg_id: int):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM delivered_messages WHERE id = ?", (msg_id,)).fetchone()
        return dict(row) if row else None


def mark_notion_synced(msg_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE delivered_messages SET notion_synced = 1 WHERE id = ?", (msg_id,))


def get_delivered_by_thread_ts(target_channel: str, thread_ts: str):
    """Look up a delivered message by the triage channel and thread timestamp."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM delivered_messages WHERE target_channel = ? AND thread_ts = ?",
            (target_channel, thread_ts)
        ).fetchone()
        return dict(row) if row else None


def get_delivered_by_thread(team_id: str, thread_ts: str):
    """Return the first delivered_messages row for a given thread."""
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT * FROM delivered_messages WHERE team_id = ? AND thread_ts = ? LIMIT 1",
            (team_id, thread_ts)
        ).fetchone()
        return dict(row) if row else None


def mark_replied(msg_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE delivered_messages SET replied = 1 WHERE id = ?", (msg_id,))


def mark_replied_and_purge_source(msg_id: int | None):
    """Mark a delivered message as replied AND purge source_channel from routing_table.

    Both operations are in a single transaction. If msg_id is None (edge case
    where delivered_messages record is missing), the routing purge is skipped
    and the startup sweep in purge_source_channels() will handle it later.
    """
    with get_conn() as conn:
        if msg_id is not None:
            row = conn.execute(
                "SELECT team_id, thread_ts FROM delivered_messages WHERE id = ?",
                (msg_id,)
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE routing_table SET source_channel = NULL "
                    "WHERE team_id = ? AND thread_ts = ?",
                    (row["team_id"], row["thread_ts"])
                )
            conn.execute(
                "UPDATE delivered_messages SET replied = 1 WHERE id = ?", (msg_id,)
            )
        print(f"[db] mark_replied_and_purge_source: msg_id={msg_id}")


def purge_delivered_source_channel(team_id: str, thread_ts: str):
    """NULL out source_channel in delivered_messages after thread close.
    Once the closure DM has been sent, source_channel is no longer needed
    and should be purged to prevent identity correlation via Slack API."""
    with get_conn() as conn:
        conn.execute(
            "UPDATE delivered_messages SET source_channel = NULL "
            "WHERE team_id = ? AND thread_ts = ?",
            (team_id, thread_ts)
        )


def purge_source_channels():
    """NULL out source_channel in routing_table for all already-replied messages.

    Called on startup as a safety sweep to catch any records that slipped
    through without a post-delivery purge.
    """
    with get_conn() as conn:
        cur = conn.execute("""
            UPDATE routing_table
            SET source_channel = NULL
            WHERE source_channel IS NOT NULL
              AND (team_id, thread_ts) IN (
                  SELECT team_id, thread_ts
                  FROM delivered_messages
                  WHERE replied = 1
              )
        """)
        if cur.rowcount:
            print(f"[db] purge_source_channels: NULLed {cur.rowcount} stale source_channel entries")
        # Safety sweep: NULL out source_channel in delivered_messages for closed threads
        conn.execute("""
            UPDATE delivered_messages
            SET source_channel = NULL
            WHERE source_channel IS NOT NULL
            AND team_id || ':' || thread_ts NOT IN (
                SELECT team_id || ':' || thread_ts FROM routing_table
            )
        """)


# ── Install nudge tracking ────────────────────────────────────────────────────

def has_nudge_been_sent(team_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM install_nudges WHERE team_id = ?", (team_id,)).fetchone()
        return row is not None

def mark_nudge_sent(team_id: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO install_nudges (team_id) VALUES (?)", (team_id,))


# ── Message mappings (edit/retract sync) ─────────────────────────────────────

def save_message_mapping(team_id: str, user_dm_ts: str, triage_thread_ts: str,
                          triage_message_ts: str, triage_channel: str):
    with get_conn() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO message_mappings
               (team_id, user_dm_ts, triage_thread_ts, triage_message_ts, triage_channel)
               VALUES (?, ?, ?, ?, ?)""",
            (team_id, user_dm_ts, triage_thread_ts, triage_message_ts, triage_channel)
        )


def get_message_mapping(team_id: str, user_dm_ts: str):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM message_mappings WHERE team_id = ? AND user_dm_ts = ?",
            (team_id, user_dm_ts)
        ).fetchone()
        return dict(row) if row else None
