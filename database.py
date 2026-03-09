"""
database.py — HushAsk SQLite layer
"""

import sqlite3, os, secrets
from datetime import datetime, timezone

DB_PATH    = os.environ.get("DB_PATH", "hushask.db")
FREE_LIMIT = int(os.environ.get("FREE_LIMIT", "20"))


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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
                created_at     TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS delivered_messages (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                team_id        TEXT NOT NULL,
                target_channel TEXT NOT NULL,
                route_type     TEXT NOT NULL,
                message        TEXT NOT NULL,
                user_hash      TEXT NOT NULL,
                notion_synced  INTEGER DEFAULT 0,
                delivered_at   TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS install_nudges (
                team_id    TEXT PRIMARY KEY,
                sent_at    TEXT DEFAULT (datetime('now'))
            );
        """)
    print("[db] Initialized.")


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
        return conn.execute(
            "SELECT * FROM workspace_config WHERE workspace_id = ?", (workspace_id,)
        ).fetchone()


def save_workspace_config(workspace_id: str, installer_id: str,
                           public_channel: str, hr_channel: str,
                           notion_api_key: str = None, notion_database_id: str = None):
    with get_conn() as conn:
        conn.execute("""
            INSERT INTO workspace_config
                (workspace_id, installer_id, public_channel, hr_channel,
                 notion_api_key, notion_database_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(workspace_id) DO UPDATE SET
                public_channel     = excluded.public_channel,
                hr_channel         = excluded.hr_channel,
                notion_api_key     = excluded.notion_api_key,
                notion_database_id = excluded.notion_database_id,
                updated_at         = excluded.updated_at
        """, (workspace_id, installer_id, public_channel, hr_channel,
              notion_api_key, notion_database_id))


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
    with get_conn() as conn:
        conn.execute("DELETE FROM workspace_config WHERE workspace_id = ?", (workspace_id,))


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

def save_pending(token, team_id, source_channel, message, user_hash, message_ts=None):
    with get_conn() as conn:
        conn.execute("""
            INSERT OR REPLACE INTO pending_messages
                (token, team_id, source_channel, message, user_hash, message_ts)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (token, team_id, source_channel, message, user_hash, message_ts))


def get_pending(token):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM pending_messages WHERE token = ?", (token,)).fetchone()


def delete_pending(token):
    with get_conn() as conn:
        conn.execute("DELETE FROM pending_messages WHERE token = ?", (token,))


def log_delivered(team_id, target_channel, route_type, message, user_hash) -> int:
    with get_conn() as conn:
        cur = conn.execute("""
            INSERT INTO delivered_messages (team_id, target_channel, route_type, message, user_hash)
            VALUES (?, ?, ?, ?, ?)
        """, (team_id, target_channel, route_type, message, user_hash))
        return cur.lastrowid


def get_delivered(msg_id: int):
    with get_conn() as conn:
        return conn.execute("SELECT * FROM delivered_messages WHERE id = ?", (msg_id,)).fetchone()


def mark_notion_synced(msg_id: int):
    with get_conn() as conn:
        conn.execute("UPDATE delivered_messages SET notion_synced = 1 WHERE id = ?", (msg_id,))


# ── Install nudge tracking ────────────────────────────────────────────────────

def has_nudge_been_sent(team_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute("SELECT 1 FROM install_nudges WHERE team_id = ?", (team_id,)).fetchone()
        return row is not None

def mark_nudge_sent(team_id: str):
    with get_conn() as conn:
        conn.execute("INSERT OR IGNORE INTO install_nudges (team_id) VALUES (?)", (team_id,))
