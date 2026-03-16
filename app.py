"""
app.py — HushAsk Slack bot
HTTP Events API mode (multi-tenant, Railway-hosted)
- SQLite-backed InstallationStore + OAuthStateStore
- 3-step setup wizard (conditional UI via views_update)
- Non-admin welcome screen with clickable examples
- Notion OAuth
- Freemium 20 msg/month cap (bypassed for Pro)
"""

import os, json, hashlib, secrets, time, re, logging, unicodedata
from threading import Lock
import urllib.parse
import requests as http
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

# ── Zero-knowledge log scrubbing ──────────────────────────────────────────────

class RedactUserIdFilter(logging.Filter):
    """Redact Slack User IDs (U + 8-11 alphanumeric chars) from all log output."""
    _pattern = re.compile(r'\bU[A-Z0-9]{8,11}\b')

    def filter(self, record):
        record.msg = self._pattern.sub('[REDACTED_USER]', str(record.msg))
        if record.args:
            try:
                record.msg = record.msg % record.args
            except Exception:
                pass
            record.args = None
        return True

_redact_filter = RedactUserIdFilter()
logging.getLogger().addFilter(_redact_filter)
logging.getLogger("slack_bolt").addFilter(_redact_filter)
logging.getLogger("slack_sdk").addFilter(_redact_filter)

logger = logging.getLogger(__name__)

from slack_bolt import App
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.installation_store import InstallationStore
from slack_sdk.oauth.installation_store.models.installation import Installation
from slack_sdk.oauth.state_store import OAuthStateStore
from slack_sdk.errors import SlackApiError

from database import (
    init_db, get_conn,
    save_workspace, find_bot_token, is_workspace_pro,
    issue_slack_state, consume_slack_state,
    save_pending, get_pending, delete_pending, claim_pending, peek_pending,
    log_delivered, get_delivered, mark_notion_synced,
    get_delivered_by_thread_ts, mark_replied, mark_replied_and_purge_source,
    save_routing, get_routing, get_active_thread_for_user,
    get_workspace_config, save_workspace_config, reset_workspace_config,
    save_workspace_notion, store_notion_state, get_team_from_state, delete_notion_state,
    check_and_increment, get_usage, close_thread,
)

# ── Config ────────────────────────────────────────────────────────────────────

HASH_SALT = os.environ.get("HASH_SALT")
if not HASH_SALT or HASH_SALT == "hushask-v1-salt":
    raise RuntimeError(
        "[HushAsk] FATAL: HASH_SALT environment variable is required and must not use the "
        "default value. Set a cryptographically random secret (e.g. secrets.token_hex(32)) "
        "in your deployment environment."
    )
FREE_LIMIT       = int(os.environ.get("FREE_LIMIT", "20"))
API_BASE         = os.environ.get("API_BASE", "https://api.hushask.com")
HELP_BASE        = os.environ.get("HELP_BASE", "https://hushask.com/help")
UPGRADE_URL      = os.environ.get("UPGRADE_URL", "https://hushask.com/upgrade")
NOTION_CLIENT_ID = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_REDIRECT  = os.environ.get("NOTION_REDIRECT_URI", f"{API_BASE}/notion/callback")


# ── SQLite-backed OAuth stores ────────────────────────────────────────────────

class SQLiteInstallationStore(InstallationStore):
    def save(self, installation: Installation):
        save_workspace(
            team_id=installation.team_id or "",
            enterprise_id=installation.enterprise_id or "",
            team_name=installation.team_name or "",
            bot_token=installation.bot_token or "",
            bot_user_id=installation.bot_user_id,
            app_id=installation.app_id,
            installer_user_id=installation.user_id,
        )

    def find_installation(self, *, enterprise_id, team_id, is_enterprise_install=False, user_id=None):
        # Must return Installation (not Bot) — Bolt middleware reads .user_token on this object
        from database import find_workspace_row
        row = find_workspace_row(team_id)
        if not row or not row.get("bot_token"):
            return None
        bot_uid = row.get("bot_user_id") or ""
        return Installation(
            app_id=row.get("app_id") or os.environ.get("SLACK_APP_ID", ""),
            enterprise_id=enterprise_id or "",
            team_id=team_id,
            bot_token=row["bot_token"],
            bot_id=bot_uid,
            bot_user_id=bot_uid,
            bot_scopes=[],
            user_id=row.get("installer_user_id") or "",
            user_token=None,      # we don't store user tokens — bot-only app
            installed_at=datetime.now(timezone.utc),
        )

    def find_bot(self, *, enterprise_id, team_id, is_enterprise_install=False):
        from slack_sdk.oauth.installation_store.models.bot import Bot
        from database import find_workspace_row
        row = find_workspace_row(team_id)
        if not row or not row.get("bot_token"):
            return None
        bot_uid = row.get("bot_user_id") or ""
        return Bot(
            app_id=row.get("app_id") or os.environ.get("SLACK_APP_ID", ""),
            enterprise_id=enterprise_id or "",
            team_id=team_id,
            bot_token=row["bot_token"],
            bot_id=bot_uid,
            bot_user_id=bot_uid,
            bot_scopes=[],
            installed_at=datetime.now(timezone.utc),
        )


class SQLiteOAuthStateStore(OAuthStateStore):
    def issue(self, *args, **kwargs) -> str:
        return issue_slack_state()

    def consume(self, state: str) -> bool:
        return consume_slack_state(state)


# ── Bolt App (multi-tenant HTTP mode) ─────────────────────────────────────────

_installation_store = SQLiteInstallationStore()

app = App(
    signing_secret=os.environ["SLACK_SIGNING_SECRET"],
    oauth_settings=OAuthSettings(
        client_id=os.environ["SLACK_CLIENT_ID"],
        client_secret=os.environ["SLACK_CLIENT_SECRET"],
        scopes=[
            "chat:write", "chat:write.public",
            "channels:read", "channels:history", "channels:manage",
            "groups:read", "groups:write", "groups:history",
            "im:history", "im:read", "im:write",
            "app_mentions:read", "users:read",
        ],
        installation_store=_installation_store,
        state_store=SQLiteOAuthStateStore(),
        redirect_uri=f"{API_BASE}/slack/oauth_redirect",
        install_page_rendering_enabled=False,
    ),
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_user(user_id, team_id):
    # Full 256-bit SHA-256 — no truncation (breaking change: existing [:16] hashes won't match)
    return hashlib.sha256(f"{HASH_SALT}:{team_id}:{user_id}".encode()).hexdigest()

def make_token(user_id, team_id):
    return hashlib.sha256(
        f"{user_id}:{team_id}:{time.time()}:{secrets.token_hex(8)}".encode()
    ).hexdigest()[:32]

def is_admin(client, user_id):
    try:
        u = client.users_info(user=user_id)["user"]
        return bool(u.get("is_admin") or u.get("is_owner") or u.get("is_primary_owner"))
    except:
        return False

def channel_display(client, cid):
    """Return '#name' for a channel ID, or the raw ID if lookup fails."""
    if not cid: return "—"
    try:
        info = client.conversations_info(channel=cid)
        if info.get("ok"):
            return f"#{info['channel']['name']}"
        print(f"[channel_display] conversations_info not ok for {cid}: {info.get('error')}")
        return cid
    except Exception as e:
        print(f"[channel_display] exception for {cid}: {e}")
        return cid

def channels_are_valid(client, pub_ch, hr_ch):
    """Return True if both channel IDs exist and the bot can access them."""
    if not pub_ch or not hr_ch:
        return False
    for cid in (pub_ch, hr_ch):
        try:
            info = client.conversations_info(channel=cid)
            if not info.get("ok"):
                print(f"[channels] validation FAIL for {cid}: {info.get('error')}")
                return False
        except Exception as e:
            print(f"[channels] validation exception for {cid}: {e}")
            return False
    return True

def find_or_create_channel(client, name, is_private):
    """Return (channel_id, error_message) for `name`, creating it if needed.

    Strategy: CREATE first (O(1)), fall back to a single-page LIST only on
    name_taken. Never paginate.

    Returns (channel_id, None) on success.
    Returns (None, user-facing error string) on failure.
    """
    ctype = "private_channel" if is_private else "public_channel"

    # ── Try to create first ──────────────────────────────────────────────────
    try:
        result = client.conversations_create(name=name, is_private=is_private)
        if result.get("ok"):
            ch_id = result["channel"]["id"]
            print(f"[channels] ✅ created '#{name}': {ch_id} (private={is_private})")
            return ch_id, None
        error = result.get("error", "unknown")
        print(f"[channels] conversations_create: '#{name}' error={error}")
        if error not in ("name_taken",):
            if error in ("missing_scope", "not_allowed_token_type", "restricted_action"):
                msg = f"Cannot create #{name} (`{error}`). Bot needs `channels:manage` and `groups:write` scopes."
            else:
                msg = f"Failed to create #{name}: `{error}`."
            print(f"[channels] ❌ {msg}")
            return None, msg
        # name_taken → channel already exists; find it
    except SlackApiError as e:
        error = e.response.get("error", "unknown")
        print(f"[channels] conversations_create SlackApiError for '#{name}': {error}")
        if error != "name_taken":
            if error in ("missing_scope", "not_allowed_token_type", "restricted_action"):
                msg = f"Cannot create #{name} (`{error}`). Bot needs `channels:manage` and `groups:write` scopes."
            else:
                msg = f"Failed to create #{name}: `{error}`."
            print(f"[channels] ❌ {msg}")
            return None, msg
        # name_taken → fall through to list scan below
    except Exception as e:
        print(f"[channels] conversations_create exception for '#{name}': {e}")
        return None, f"Exception creating #{name}: {e}"

    # ── name_taken: single-page scan ────────────────────────────────────────
    try:
        resp = client.conversations_list(types=ctype, limit=200, exclude_archived=True)
        if resp.get("ok"):
            match = next((c for c in resp.get("channels", []) if c["name"] == name), None)
            if match:
                print(f"[channels] ✅ '#{name}' exists: {match['id']} (type={ctype})")
                return match["id"], None
            # Not visible — for private channels this means the bot isn't a member
            if is_private:
                msg = (f"`#{name}` exists but bot is not a member. "
                       f"Run `/invite @HushAsk` then retry Setup.")
            else:
                msg = f"`#{name}` exists but isn't accessible. Check bot permissions."
            print(f"[channels] ⚠️ {msg}")
            return None, msg
        else:
            msg = f"conversations.list failed for #{name}: `{resp.get('error')}`"
            print(f"[channels] ❌ {msg}")
            return None, msg
    except Exception as e:
        print(f"[channels] conversations_list exception for '#{name}': {e}")
        return None, f"Exception scanning for #{name}: {e}"

def upgrade_link(team_id):
    return f"{API_BASE}/upgrade?team_id={team_id}"


# ── Workspace display-name cache (for safety filter) ─────────────────────────

_display_name_cache: dict = {}     # team_id → {"names": [...], "fetched_at": float}
_display_name_fetching: dict = {}  # team_id → bool; guards against cache stampede
_display_name_lock = Lock()
_DISPLAY_NAME_TTL  = 14400  # refresh every 4 hours


def normalize_for_name_check(text: str) -> str:
    """NFKD-normalize, strip non-alpha chars, lowercase.

    Handles Unicode homoglyphs and diacritics so name detection isn't
    bypassable with accented/full-width characters.
    """
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_only = nfkd.encode("ascii", "ignore").decode("ascii")
    stripped = re.sub(r"[^a-z]", " ", ascii_only.lower())
    return stripped


def get_workspace_display_names(client, team_id: str) -> list:
    """Return lowercased display/real names for all non-bot, non-deleted members.

    Results are cached per team_id and refreshed every 4 hours.
    Uses a double-checked lock to prevent cache stampedes: only one thread
    fetches per team_id at a time; others wait up to 5 s then return stale/empty.
    """
    # Fast path — return cached entry if still fresh
    with _display_name_lock:
        entry = _display_name_cache.get(team_id)
        if entry and (time.time() - entry["fetched_at"]) < _DISPLAY_NAME_TTL:
            return entry["names"]

        # If another thread is already fetching for this team, don't double-fetch
        if _display_name_fetching.get(team_id):
            should_fetch = False
        else:
            _display_name_fetching[team_id] = True
            should_fetch = True

    if not should_fetch:
        # Wait up to 5 s for the in-flight fetch to finish, then return whatever is cached
        deadline = time.time() + 5
        while time.time() < deadline:
            with _display_name_lock:
                if not _display_name_fetching.get(team_id):
                    return _display_name_cache.get(team_id, {}).get("names", [])
            time.sleep(0.1)
        # Timeout — return stale cache if available, else empty list
        with _display_name_lock:
            return _display_name_cache.get(team_id, {}).get("names", [])

    # We claimed the fetch slot — go get the data
    names = []
    try:
        cursor = None
        while True:
            kwargs = {"limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.users_list(**kwargs)
            if not resp.get("ok"):
                print(f"[display_names] users_list error: {resp.get('error')}")
                break
            for member in resp.get("members", []):
                if member.get("deleted") or member.get("is_bot") or member.get("id") == "USLACKBOT":
                    continue
                profile = member.get("profile", {})
                dn = normalize_for_name_check(profile.get("display_name") or "").strip()
                rn = normalize_for_name_check(profile.get("real_name")    or "").strip()
                if dn and len(dn) >= 3:
                    names.append(dn)
                if rn and len(rn) >= 3 and rn != dn:
                    names.append(rn)
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except Exception as e:
        print(f"[display_names] Exception fetching for {team_id}: {e}")
    finally:
        with _display_name_lock:
            _display_name_cache[team_id] = {"names": names, "fetched_at": time.time()}
            _display_name_fetching.pop(team_id, None)

    return names


# ── Notion ────────────────────────────────────────────────────────────────────

def push_to_notion(token, database_id, message, route_type):
    label = "📢 Public" if route_type == "public" else "🔒 Confidential / HR"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": message[:80] + ("…" if len(message) > 80 else "")}}]},
            "Route": {"select": {"name": label}},
            "Status": {"select": {"name": "New"}},
            "Synced At": {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")}},
        },
        "children": [
            {"object":"block","type":"callout","callout":{"rich_text":[{"type":"text","text":{"content":f"Route: {label} · Sender identity protected 🔒"}}],"icon":{"emoji":"🔒"},"color":"gray_background"}},
            {"object":"block","type":"heading_2","heading_2":{"rich_text":[{"type":"text","text":{"content":"Anonymous Message"}}]}},
            {"object":"block","type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content":message}}]}},
            {"object":"block","type":"divider","divider":{}},
            {"object":"block","type":"paragraph","paragraph":{"rich_text":[{"type":"text","text":{"content":"Add your answer below ↓"},"annotations":{"italic":True,"color":"gray"}}]}},
        ]
    }
    last_err = None
    for attempt in range(3):
        try:
            r = http.post("https://api.notion.com/v1/pages", json=payload, headers=headers, timeout=10)
            if r.status_code == 200:
                return True, None
            if r.status_code in (429, 500, 502, 503, 504):
                last_err = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)  # 1s, 2s, 4s
                continue
            return False, r.json().get("message", f"Notion API error: {r.status_code} {r.text[:200]}")
        except http.RequestException as e:
            last_err = str(e)
            time.sleep(2 ** attempt)
    return False, f"Notion push failed after 3 attempts: {last_err}"


# ── Block builders ────────────────────────────────────────────────────────────

EXAMPLE_MESSAGES = {
    "example_tech":     "Our deploy process feels fragile — has anyone proposed a more reliable approach?",
    "example_feedback": "I'd like to discuss my compensation but I'm not sure who to talk to or how to start.",
    "example_idea":     "What if we ran a quarterly retrospective open to every team, not just engineering?",
}

ONBOARDING_BLOCKS = [
    {"type": "section", "text": {"type": "mrkdwn", "text": "HushAsk: Anonymous message routing for Slack. Route to Public or Confidential HR. Synced to Notion."}},
    {"type": "context", "elements": [{"type": "mrkdwn", "text": "🤫 Your identity is never stored."}]},
]

def routing_blocks(token, message):
    preview = message[:100] + "…" if len(message) > 100 else message
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Message received. Select a route:*\n>{preview}"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Your Slack identity is not stored or logged."}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": "route_hr", "style": "primary",
             "text": {"type": "plain_text", "text": "🔒 Confidential / HR"}, "value": token},
            {"type": "button", "action_id": "route_public",
             "text": {"type": "plain_text", "text": "Public / Knowledge Base"}, "value": token},
        ]},
        {"type": "context", "elements": [
            {"type": "mrkdwn", "text": "🤫 Your identity is never stored."}
        ]},
    ]

def route_confirmation_blocks(token: str, route_type: str, message: str) -> list:
    route_labels = {
        "public": "Public / Knowledge Base",
        "hr": "Confidential / HR",
    }
    label = route_labels.get(route_type, route_type)
    preview = message[:100] + "…" if len(message) > 100 else message
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"You selected: *{label}*. Please confirm before sending.\n>{preview}"
            }
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "action_id": "route_confirm",
                    "style": "primary",
                    "text": {"type": "plain_text", "text": "Confirm & Send", "emoji": False},
                    "value": f"{token}|{route_type}"
                },
                {
                    "type": "button",
                    "action_id": "route_cancel",
                    "style": "danger",
                    "text": {"type": "plain_text", "text": "Cancel", "emoji": False},
                    "value": token
                }
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🤫 Your identity is never stored."}]
        }
    ]

def confirmed_blocks(label):
    return [{"type":"section","text":{"type":"mrkdwn","text":f"Sent. Routed to: *{label}*"}}]

def triage_blocks(message, label, msg_id, has_notion):
    blocks = [{"type":"section","text":{"type":"mrkdwn","text":f"{label}\n\n{message}"}}]
    if has_notion:
        blocks.append({"type":"actions","elements":[{"type":"button","action_id":"sync_notion","text":{"type":"plain_text","text":"📄 Sync to Notion","emoji":True},"value":str(msg_id)}]})
    blocks.append({"type":"context","elements":[{"type":"mrkdwn","text":"🔒 Anonymous · HushAsk"}]})
    return blocks

def limit_blocks(usage, team_id=""):
    url = upgrade_link(team_id) if team_id else UPGRADE_URL
    return [
        {"type":"section","text":{"type":"mrkdwn","text":f"Free tier cap reached: *{usage}/{FREE_LIMIT}* messages this month.\n\nUpgrade to remove the limit."}},
        {"type":"actions","elements":[{"type":"button","action_id":"upgrade_click","style":"primary","text":{"type":"plain_text","text":"Upgrade to Pro","emoji":True},"url":url}]},
        {"type":"context","elements":[{"type":"mrkdwn","text":"Resets monthly."}]}
    ]

def _alert_installer_limit(client, team_id: str, usage: int):
    """DM the workspace installer once when the free tier cap is first hit."""
    try:
        from database import find_installer_user_id
        installer_id = find_installer_user_id(team_id)
        if not installer_id:
            return
        dm = client.conversations_open(users=installer_id)["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            text=f"Free tier cap hit: {usage}/{FREE_LIMIT} messages this month.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    f"*Free tier cap: {usage}/{FREE_LIMIT}.*\n\n"
                    f"New submissions paused. Upgrade to restore routing, or wait for monthly reset."
                }},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "Upgrade to Pro"},
                     "style": "primary", "url": f"{UPGRADE_URL}?team_id={team_id}",
                     "action_id": "upgrade_cta_admin_alert"}
                ]}
            ]
        )
        print(f"[limit_alert] Sent admin DM to {installer_id} for workspace {team_id} ({usage}/{FREE_LIMIT})")
    except Exception as e:
        print(f"[limit_alert] Failed to DM installer for {team_id}: {e}")


def pro_welcome_blocks():
    return [
        {"type":"header","text":{"type":"plain_text","text":"HushAsk Pro — Active","emoji":True}},
        {"type":"section","text":{"type":"mrkdwn","text":"Pro plan active. What changed:\n\n✅ No message cap\n✅ Notion sync\n✅ Multi-channel routing\n✅ Priority support"}},
        {"type":"divider"},
        {"type":"context","elements":[{"type":"mrkdwn","text":"Support: hello@hushask.com"}]}
    ]


# ── App Home views ────────────────────────────────────────────────────────────

def home_welcome():
    return {
        "type":"home","blocks":[
            {"type":"header","text":{"type":"plain_text","text":"HushAsk","emoji":True}},
            {"type":"section","text":{"type":"mrkdwn","text":"Anonymous message routing for your team."}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"Send any question, idea, or concern to the right channel — anonymously. Identity is hashed and never stored."}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"Examples — click to route through the bot:"}},
            {"type":"section","text":{"type":"mrkdwn","text":"💻 *Tech*\n_\"Our deploy process feels fragile — has anyone proposed a more reliable approach?\"_"},"accessory":{"type":"button","action_id":"example_tech","text":{"type":"plain_text","text":"Send this","emoji":True},"style":"primary"}},
            {"type":"section","text":{"type":"mrkdwn","text":"🧑‍💼 *HR*\n_\"I'd like to discuss my compensation but I'm not sure who to talk to.\"_"},"accessory":{"type":"button","action_id":"example_feedback","text":{"type":"plain_text","text":"Send this","emoji":True}}},
            {"type":"section","text":{"type":"mrkdwn","text":"💡 *Idea*\n_\"What if we ran a quarterly retrospective open to every team, not just engineering?\"_"},"accessory":{"type":"button","action_id":"example_idea","text":{"type":"plain_text","text":"Send this","emoji":True}}},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":f"🔒 Slack ID is SHA-256 hashed — never stored in plaintext. · <{HELP_BASE}/privacy-and-hashing.html|Learn more>"}]}
        ]
    }

def home_unconfigured():
    return {
        "type":"home","blocks":[
            {"type":"header","text":{"type":"plain_text","text":"HushAsk Setup","emoji":True}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"Not configured yet. Run the setup wizard to define routing channels and optional Notion sync."}},
            {"type":"actions","elements":[{"type":"button","action_id":"start_setup","style":"primary","text":{"type":"plain_text","text":"Start Setup","emoji":True}}]},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":"Admins and installer only."}]}
        ]
    }

def home_configured(config, client, team_id):
    pub   = channel_display(client, config["public_channel"])
    hr    = channel_display(client, config["hr_channel"])
    pro   = is_workspace_pro(team_id)
    n_key = config["notion_api_key"]
    n_db  = config["notion_database_id"]
    notion = "✅ Hush Library connected" if (n_key and n_db) else ("🔑 Token only" if n_key else "⬜ Not connected")
    usage = get_usage(team_id)
    tier  = "⭐ Pro — unlimited" if pro else f"{usage} / {FREE_LIMIT}"
    pct   = min(int((usage / FREE_LIMIT) * 100), 100)
    bar   = ("🟧" * (pct // 20)) + ("⬜" * (5 - pct // 20))

    blocks = [
        {"type":"header","text":{"type":"plain_text","text":"HushAsk","emoji":True}},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":"*Configuration*"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*📢 Public*\n{pub}"},
            {"type":"mrkdwn","text":f"*🔒 Private*\n{hr}"},
            {"type":"mrkdwn","text":f"*📄 Notion*\n{notion}"},
            {"type":"mrkdwn","text":f"*Usage*\n{tier}"},
        ]},
    ]

    if not pro:
        blocks.append({
            "type":"section",
            "text":{"type":"mrkdwn","text":f"*Monthly usage* {bar}  {pct}%"}
        })

    buttons = [
        {"type":"button","action_id":"edit_settings","style":"primary","text":{"type":"plain_text","text":"Edit","emoji":True}},
        {"type":"button","action_id":"reset_config","style":"danger","text":{"type":"plain_text","text":"Reset","emoji":True},"confirm":{"title":{"type":"plain_text","text":"Reset configuration?"},"text":{"type":"mrkdwn","text":"Clears routing and Notion settings. Message history preserved."},"confirm":{"type":"plain_text","text":"Reset"},"deny":{"type":"plain_text","text":"Cancel"},"style":"danger"}},
    ]
    if not pro:
        buttons.append({"type":"button","action_id":"upgrade_click","text":{"type":"plain_text","text":"Upgrade to Pro","emoji":True},"url":upgrade_link(team_id),"style":"primary"})

    blocks += [
        {"type":"actions","elements":buttons},
        {"type":"divider"},
        {"type":"context","elements":[
            {"type":"mrkdwn","text":f"<{HELP_BASE}/|Help> · {'⭐ Pro' if pro else 'Free'} · `Build: {BUILD_ID}`"}
        ]}
    ]
    return {"type":"home","blocks":blocks}

BUILD_ID = "sovereign-v1"  # git short SHA — update on each deploy for UI verification

def publish_home(client, user_id, team_id):
    """Publish the App Home tab for a user.

    IMPORTANT ordering: is_admin() is an API call that takes ~1-3s.
    We do it FIRST, then read the DB — this way any concurrent wizard3
    write has time to commit before we snapshot config.
    """
    from database import DB_PATH

    # ── Step 1: resolve admin status (slow API call) ─────────────────────────
    try:
        admin = is_admin(client, user_id)
    except Exception as e:
        print(f"[publish_home] is_admin failed ({e}) — defaulting False")
        admin = False

    # ── Step 2: read DB AFTER is_admin — catches concurrent wizard3 writes ───
    config = get_workspace_config(team_id)

    # Safety net: if config is still empty, wait briefly and retry once.
    # This handles the rare case where is_admin() returned in <100ms.
    if config is None:
        import time; time.sleep(0.5)
        config = get_workspace_config(team_id)

    installer_id  = config["installer_id"]       if config else None
    pub_ch        = config["public_channel"]      if config else None
    hr_ch         = config["hr_channel"]          if config else None
    notion_key    = config["notion_api_key"]      if config else None
    notion_db     = config["notion_database_id"]  if config else None
    is_configured = bool(pub_ch and hr_ch)

    print(f"[publish_home] build={BUILD_ID} user={re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(user_id))} team={team_id} db={DB_PATH} | "
          f"configured={is_configured} pub={pub_ch} hr={hr_ch} "
          f"notion_key={bool(notion_key)} notion_db={bool(notion_db)} installer={re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(installer_id))}")

    is_privileged = admin or user_id == installer_id or (installer_id is None and config is not None)

    if is_privileged or is_configured:
        view = home_configured(config, client, team_id) if is_configured else home_unconfigured()
    else:
        view = home_welcome()
    client.views_publish(user_id=user_id, view=view)


# ── Wizard modals ─────────────────────────────────────────────────────────────

def wizard_step1():
    return {
        "type":"modal","callback_id":"wizard_step1",
        "title":{"type":"plain_text","text":"Setup (1/3)"},
        "submit":{"type":"plain_text","text":"Next →"},
        "close":{"type":"plain_text","text":"Cancel"},
        "blocks":[
            {"type":"header","text":{"type":"plain_text","text":"How HushAsk works","emoji":True}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"Anonymous message routing, with optional Notion sync.\n\n*Flow:*"}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Step 1 — Submit*\nAn employee DMs the bot with a question or feedback."}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Step 2 — Route*\nThey select a destination: Public Knowledge Base or Confidential HR."}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Step 3 — Resolve*\nA leader replies in the triage channel. The employee receives the answer anonymously via DM."}},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":"Notion step is optional. ~2 minutes total."}]}
        ]
    }

def wizard_step2_modal(auto_create=True, meta=None):
    if meta is None: meta = {}
    auto_el = {
        "type":"checkboxes","action_id":"auto_create_check",
        "options":[{"text":{"type":"mrkdwn","text":"*Auto-create channels*\nCreates `#hush-public` and `#hush-hr`."},"value":"auto_create"}],
    }
    if auto_create:
        auto_el["initial_options"] = [{"text":{"type":"mrkdwn","text":"*Auto-create channels*\nCreates `#hush-public` and `#hush-hr`."},"value":"auto_create"}]

    blocks = [
        {"type":"header","text":{"type":"plain_text","text":"Triage Channels"}},
        {"type":"section","text":{"type":"mrkdwn","text":"Two channels required: *Public* (general) and *Private* (HR/confidential)."}},
        {"type":"divider"},
        {"type":"input","block_id":"block_auto_create","dispatch_action":True,"label":{"type":"plain_text","text":"Channel setup"},"optional":True,"element":auto_el},
    ]
    if auto_create:
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":"`#hush-public` and `#hush-hr` will be created if they don't exist. Uncheck to select existing channels."}})
    else:
        # No type filter — show ALL channels the user can access.
        # Filtering by "private" only shows channels the bot is already in,
        # which is an empty list before setup. Let the user pick any channel.
        pub_el = {
            "type": "conversations_select",
            "action_id": "public_channel_select",
            "placeholder": {"type": "plain_text", "text": "Pick a public channel"},
            "filter": {"include": ["public"], "exclude_bot_users": True},
        }
        hr_el = {
            "type": "conversations_select",
            "action_id": "hr_channel_select",
            "placeholder": {"type": "plain_text", "text": "Pick any channel for HR/private"},
            # No private filter — bot won't be in private channels yet.
            # User picks any channel; they should invite the bot separately.
        }
        if meta.get("public_channel"): pub_el["initial_conversation"] = meta["public_channel"]
        if meta.get("hr_channel"):     hr_el["initial_conversation"]  = meta["hr_channel"]
        blocks += [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "Bot must be a member of both channels. Run `/invite @HushAsk` after setup."}},
            {"type": "input", "block_id": "block_public_channel",
             "label": {"type": "plain_text", "text": "Public Channel"},
             "hint":  {"type": "plain_text", "text": "Public anonymous messages land here."},
             "optional": False, "element": pub_el},
            {"type": "input", "block_id": "block_hr_channel",
             "label": {"type": "plain_text", "text": "Private / HR Channel"},
             "hint":  {"type": "plain_text", "text": "Confidential messages. Bot must be a member first."},
             "optional": False, "element": hr_el},
        ]
    # Always show HR leaders selector (optional) — invited to private HR channel on create
    blocks.append({
        "type": "input",
        "block_id": "hr_leaders",
        "optional": True,
        "label": {"type": "plain_text", "text": "Additional HR Leaders (optional)", "emoji": False},
        "hint": {"type": "plain_text", "text": "These users will be invited to the confidential HR channel.", "emoji": False},
        "element": {
            "type": "multi_users_select",
            "action_id": "hr_leaders_input",
            "placeholder": {"type": "plain_text", "text": "Select team members", "emoji": False},
        },
    })
    return {
        "type":"modal","callback_id":"wizard_step2",
        "private_metadata":json.dumps(meta),
        "title":{"type":"plain_text","text":"Setup (2/3)"},
        "submit":{"type":"plain_text","text":"Continue →"},
        "close":{"type":"plain_text","text":"Back"},
        "blocks":blocks
    }

def wizard_step3(meta):
    has_oauth = bool(NOTION_CLIENT_ID)
    notion_state = meta.get("notion_state", "")
    if has_oauth:
        oauth_url = (f"https://api.notion.com/v1/oauth/authorize?client_id={NOTION_CLIENT_ID}"
                     f"&response_type=code&owner=user&redirect_uri={urllib.parse.quote(NOTION_REDIRECT, safe='')}&state={notion_state}")
        vault_blocks = [
            {"type":"section","text":{"type":"mrkdwn","text":f"Authorize HushAsk to create a *Hush Library* database in Notion. No manual setup needed. <{HELP_BASE}/setting-up-notion.html|Setup guide →>"}},
            {"type":"divider"},
            {"type":"actions","elements":[{"type":"button","action_id":"notion_oauth_click","style":"primary","text":{"type":"plain_text","text":"Connect Notion","emoji":True},"url":oauth_url}]},
            {"type":"context","elements":[{"type":"mrkdwn","text":"After authorizing in your browser, return here and click Save & Finish."}]}
        ]
    else:
        vault_blocks = [
            {"type":"section","text":{"type":"mrkdwn","text":f"Enter your Notion token and database ID. <{HELP_BASE}/setting-up-notion.html|Setup guide →>"}},
            {"type":"divider"},
            {"type":"input","block_id":"block_notion_token","label":{"type":"plain_text","text":"Notion API Token"},"optional":True,"element":{"type":"plain_text_input","action_id":"notion_token_input","placeholder":{"type":"plain_text","text":"secret_..."},"initial_value":meta.get("notion_api_key","")}},
            {"type":"input","block_id":"block_notion_db","label":{"type":"plain_text","text":"Database ID"},"optional":True,"element":{"type":"plain_text_input","action_id":"notion_db_input","placeholder":{"type":"plain_text","text":"32-char ID"},"initial_value":meta.get("notion_database_id","")}},
            {"type":"context","elements":[{"type":"mrkdwn","text":"Optional. Configure later from Settings."}]}
        ]
    return {
        "type":"modal","callback_id":"wizard_step3",
        "private_metadata":json.dumps(meta),
        "title":{"type":"plain_text","text":"Setup (3/3)"},
        "submit":{"type":"plain_text","text":"Save & Finish"},
        "close":{"type":"plain_text","text":"Back"},
        "blocks":[{"type":"header","text":{"type":"plain_text","text":"Notion Sync (optional)","emoji":True}},*vault_blocks]
    }


def settings_modal(config: dict) -> dict:
    cfg = config or {}

    pub_el = {
        "type": "conversations_select",
        "action_id": "public_channel_setting_input",
        "placeholder": {"type": "plain_text", "text": "Select a channel", "emoji": False},
        "filter": {"include": ["public_channel"]},
    }
    if cfg.get("public_channel"):
        pub_el["initial_conversation"] = cfg["public_channel"]

    hr_el = {
        "type": "conversations_select",
        "action_id": "hr_channel_setting_input",
        "placeholder": {"type": "plain_text", "text": "Select a channel", "emoji": False},
        "filter": {"include": ["private_channel"]},
    }
    if cfg.get("hr_channel"):
        hr_el["initial_conversation"] = cfg["hr_channel"]

    notion_connected = bool(cfg.get("notion_database_id"))
    notion_btn = {
        "type": "button",
        "action_id": "settings_notion_toggle",
        "text": {
            "type": "plain_text",
            "text": "Disconnect Notion" if notion_connected else "Connect Notion",
            "emoji": False,
        },
    }

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Channel Configuration", "emoji": False}},
        {
            "type": "input",
            "block_id": "public_channel_setting",
            "label": {"type": "plain_text", "text": "Public Channel", "emoji": False},
            "element": pub_el,
        },
        {
            "type": "input",
            "block_id": "hr_channel_setting",
            "label": {"type": "plain_text", "text": "HR / Confidential Channel", "emoji": False},
            "element": hr_el,
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Notion Integration*\nConnect a Notion workspace to automatically document public Q&A."},
            "accessory": notion_btn,
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🤫 Changes apply immediately to new submissions."}],
        },
    ]

    return {
        "type": "modal",
        "callback_id": "workspace_settings",
        "title": {"type": "plain_text", "text": "Workspace Settings", "emoji": False},
        "submit": {"type": "plain_text", "text": "Save Changes", "emoji": False},
        "close": {"type": "plain_text", "text": "Cancel", "emoji": False},
        "blocks": blocks,
    }


# ── App Home Block Kit view constants ─────────────────────────────────────────

# STATE 1 — Admin Setup
ADMIN_SETUP_HOME = {
    "type": "home",
    "blocks": [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "HushAsk — Workspace Setup", "emoji": False}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Configure your routing channels to activate HushAsk for your team."}
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Start Setup", "emoji": False},
                    "action_id": "start_setup",
                    "style": "primary"
                }
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🤫 HushAsk never stores message content in plain text."}]
        }
    ]
}

# STATE 2 — Standard User
def build_standard_home(is_admin: bool = False) -> dict:
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "HushAsk", "emoji": False}
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "Anonymous message routing for your team."}
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*How it works:*\n1. Send a DM to this bot with your question or feedback\n2. Select a route: Public Knowledge Base or Confidential HR\n3. A leader responds in the triage channel — you receive their reply anonymously"
            }
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Send Anonymous Message", "emoji": False},
                    "action_id": "home_send_dm_prompt",
                    "style": "primary"
                }
            ]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "🤫 Your identity is never stored or logged."}]
        }
    ]

    if is_admin:
        blocks += [
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Configuration*"}
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Update channel routing, Notion integration, or workspace settings."
                },
                "accessory": {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Open Setup Wizard", "emoji": False},
                    "action_id": "open_wizard"
                }
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "🤫 Settings changes take effect immediately."}]
            }
        ]

    return {"type": "home", "blocks": blocks}

# ── Events & Actions ──────────────────────────────────────────────────────────

@app.event("app_home_opened")
def handle_app_home_opened(event, client, logger, body):
    try:
        user_id = event["user"]
        # team_id must come from the outer body — it is NOT in the inner event payload.
        # client.team_info() requires team:read scope which this app does not request.
        team_id = body.get("team_id") or body.get("team", {}).get("id", "")
        logger.info(f"[app_home] triggered for team={team_id}, tab={event.get('tab')}")

        if not team_id:
            logger.error("[app_home] No team_id in body — cannot publish home view")
            return

        # Only publish on the Home tab (not Messages tab)
        if event.get("tab") != "home":
            return

        config = get_workspace_config(team_id)
        is_configured = bool(
            config
            and config.get("public_channel")
            and config.get("hr_channel")
        )

        if not is_configured:
            # Check if user is an admin — show setup wizard to admins only
            try:
                user_info = client.users_info(user=user_id)
                is_admin = user_info["user"].get("is_admin", False) or user_info["user"].get("is_owner", False)
            except Exception:
                is_admin = False

            if is_admin:
                view = ADMIN_SETUP_HOME
            else:
                # Non-admin, not configured: show a minimal holding screen
                view = {
                    "type": "home",
                    "blocks": [
                        {
                            "type": "section",
                            "text": {"type": "mrkdwn", "text": "HushAsk is not yet configured for this workspace. Ask your Slack admin to complete setup."}
                        },
                        {
                            "type": "context",
                            "elements": [{"type": "mrkdwn", "text": "🤫 Your identity is never stored or logged."}]
                        }
                    ]
                }
        else:
            # Configured workspace — check admin status for Settings section
            try:
                user_info = client.users_info(user=user_id)
                is_admin = (
                    user_info["user"].get("is_admin", False)
                    or user_info["user"].get("is_owner", False)
                )
            except Exception:
                is_admin = False

            view = build_standard_home(is_admin=is_admin)

        try:
            result = client.views_publish(user_id=user_id, view=view)
            logger.info(f"[app_home] views_publish result: {result}")
        except Exception as e:
            logger.error(f"[app_home] views_publish FAILED: {type(e).__name__}: {e}")
            # If it's a SlackApiError, log the full response
            if hasattr(e, 'response'):
                logger.error(f"[app_home] Slack error response: {e.response}")
            raise

        logger.info(f"[app_home] published view for user {re.sub(r'U[A-Z0-9]{{8,11}}', '[USER]', user_id)}, team {team_id}")

    except Exception as e:
        logger.error(f"[app_home] FAILED: {type(e).__name__}: {e}")
        if hasattr(e, 'response'):
            logger.error(f"[app_home] Slack response: {e.response}")


# Ack the nudge deep-link button so Slack doesn't show an error
@app.action("open_home_nudge")
def handle_open_home_nudge(ack): ack()


def _maybe_send_install_nudge(client, user_id: str, team_id: str):
    """Send a one-time setup DM when someone opens the App Home for the first time
    and the workspace has no configuration yet."""
    try:
        config = get_workspace_config(team_id)
        # sqlite3.Row doesn't support .get() — index directly with fallback
        if config and (config["public_channel"] or config["hr_channel"]):
            return  # Already configured — stay silent
        from database import has_nudge_been_sent, mark_nudge_sent
        if has_nudge_been_sent(team_id):
            return  # Already nudged
        dm = client.conversations_open(users=user_id)["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            text="HushAsk: Anonymous message routing for Slack. Route to Public or Confidential HR. Synced to Notion.",
            blocks=ONBOARDING_BLOCKS,
        )
        mark_nudge_sent(team_id)
        print(f"[install_nudge] Sent to {re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(user_id))} for workspace {team_id}")
    except Exception as e:
        print(f"[install_nudge] error: {e}")

def _open_wizard_setup(ack, body, client):
    ack()
    team_id = body["team"]["id"]
    config  = get_workspace_config(team_id)
    meta = {}
    if config:
        meta = {"public_channel": config["public_channel"] or "", "hr_channel": config["hr_channel"] or "",
                "notion_api_key": config["notion_api_key"] or "", "notion_database_id": config["notion_database_id"] or ""}
    client.views_open(trigger_id=body["trigger_id"], view=wizard_step1())

app.action("start_setup")(_open_wizard_setup)
app.action("edit_settings")(_open_wizard_setup)
# Wire App Home wizard buttons
app.action("wizard_open")(_open_wizard_setup)

@app.action("open_wizard")
def _open_wizard(ack, body, client, logger):
    ack()
    user_id = body["user"]["id"]
    team_id = body.get("team", {}).get("id") or body.get("team_id", "")
    try:
        config = get_workspace_config(team_id) if team_id else None
        client.views_open(
            trigger_id=body["trigger_id"],
            view=settings_modal(config)
        )
    except Exception as e:
        logger.error(f"[settings] failed to open settings modal: {e}")

@app.view("workspace_settings")
def handle_settings_submit(ack, body, view, client, logger):
    # Ack first — beats 3s timeout
    ack({"response_action": "clear"})

    def _do_save():
        try:
            team_id = body.get("team", {}).get("id") or body.get("team_id", "")
            state = view["state"]["values"]

            pub_ch = state.get("public_channel_setting", {}).get("public_channel_setting_input", {}).get("selected_conversation")
            hr_ch  = state.get("hr_channel_setting",    {}).get("hr_channel_setting_input",    {}).get("selected_conversation")

            if not pub_ch or not hr_ch:
                logger.warning(f"[settings] incomplete submission — pub={pub_ch} hr={hr_ch}")
                return

            # Load existing config to preserve other fields (Notion, installer, etc.)
            existing = get_workspace_config(team_id) or {}

            # save_workspace_config(workspace_id, installer_id, public_channel, hr_channel,
            #                       notion_api_key=None, notion_database_id=None)
            # Passing None for notion fields preserves existing values (safe to omit).
            save_workspace_config(
                team_id,
                existing.get("installer_id"),
                pub_ch,
                hr_ch,
                existing.get("notion_api_key"),
                existing.get("notion_database_id"),
            )
            logger.info(f"[settings] workspace config updated — pub={pub_ch} hr={hr_ch}")

        except Exception as e:
            logger.error(f"[settings] save failed: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    import threading
    threading.Thread(target=_do_save, daemon=True).start()


@app.action("settings_notion_toggle")
def handle_settings_notion_toggle(ack, body, client, logger):
    ack()
    user_id = body["user"]["id"]
    team_id = body.get("team", {}).get("id") or body.get("team_id", "")
    config  = get_workspace_config(team_id) or {}

    if config.get("notion_database_id"):
        # Disconnect: clear Notion fields via save_workspace_notion (save_workspace_config
        # uses `or` logic so passing None would silently preserve existing values)
        try:
            from database import save_workspace_notion
            save_workspace_notion(team_id, None, None)
            logger.info(f"[settings] Notion disconnected for team {team_id}")
        except Exception as e:
            logger.error(f"[settings] Notion disconnect failed: {e}")
    else:
        # Connect: send DM with OAuth link
        try:
            notion_url = f"{os.environ.get('BASE_URL', 'https://api.hushask.com')}/notion/connect?team_id={team_id}"
            client.chat_postMessage(
                channel=user_id,
                text=f"To connect Notion, visit: {notion_url}"
            )
        except Exception as e:
            logger.error(f"[settings] Notion connect DM failed: {e}")


@app.action("home_send_dm_prompt")
def handle_home_send_dm(ack, body, client, logger):
    ack()
    user_id = body["user"]["id"]
    try:
        # Open (or retrieve existing) DM with the user
        result = client.conversations_open(users=user_id)
        dm_channel = result["channel"]["id"]
        # Post the DM prompt
        client.chat_postMessage(
            channel=dm_channel,
            text="What would you like to share? Type your message and send it here.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "What would you like to share?"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Type your message and send it here. Once received, you will select a route — public knowledge base or confidential HR."
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "🤫 Your identity is never stored or logged."
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        logger.error(f"[home_cta] failed to open DM: {e}")

@app.action("reset_config")
def handle_reset(ack, body, client):
    ack()
    reset_workspace_config(body["team"]["id"])
    publish_home(client, body["user"]["id"], body["team"]["id"])

@app.action("auto_create_check")
def handle_auto_toggle(ack, body, client, logger):
    print("=== CHECKBOX HANDLER CALLED ===")
    ack()
    view = body["view"]
    view_id = view["id"]
    view_hash = view["hash"]
    selected_opts = body["actions"][0].get("selected_options") or []
    is_checked = len(selected_opts) > 0

    logger.info(f"[wizard] auto_create_check fired — checked={is_checked}, view_id={view_id}")

    try:
        meta = json.loads(view.get("private_metadata", "{}"))
    except Exception:
        meta = {}

    updated_view = wizard_step2_modal(auto_create=is_checked, meta=meta)

    try:
        logger.info(f"[wizard] calling views_update — block count: {len(updated_view.get('blocks', []))}")
        client.views_update(
            view_id=view_id,
            hash=view_hash,
            view=updated_view
        )
        logger.info(f"[wizard] views_update succeeded")
    except Exception as e:
        logger.error(f"[wizard] views_update FAILED: {type(e).__name__}: {e}")
        if hasattr(e, 'response') and e.response:
            logger.error(f"[wizard] Slack error code: {e.response.get('error', 'unknown')}")
            logger.error(f"[wizard] Slack response: {dict(e.response)}")
        raise

@app.action("notion_oauth_click")
def handle_notion_oauth_click(ack): ack()

@app.action("upgrade_click")
def handle_upgrade(ack): ack()

@app.action("upgrade_cta_admin_alert")
def handle_upgrade_cta_admin_alert(ack): ack()

@app.view("wizard_step1")
def wizard1_submit(ack):
    print("[wizard1] submitted — pushing step 2")
    ack(response_action="push", view=wizard_step2_modal(auto_create=True))

@app.view("wizard_step2")
def wizard2_submit(ack, body):
    values  = body["view"]["state"]["values"]
    meta    = json.loads(body["view"].get("private_metadata", "{}"))
    team_id = body["team"]["id"]
    print(f"[wizard2] submitted for {team_id}")

    auto_opts   = values.get("block_auto_create", {}).get("auto_create_check", {}).get("selected_options", [])
    auto_create = any(o["value"] == "auto_create" for o in auto_opts)
    pub_ch = hr_ch = ""
    if not auto_create:
        if "public_channel_select" in values:
            # Inline selector blocks (new flow — correct Slack filter types)
            pub_ch = values.get("public_channel_select", {}).get("public_channel_input", {}).get("selected_conversation") or ""
            hr_ch  = values.get("hr_channel_select",     {}).get("hr_channel_input",     {}).get("selected_conversation") or ""
            if not pub_ch or not hr_ch:
                errors = {}
                if not pub_ch: errors["public_channel_select"] = "Select a public channel."
                if not hr_ch:  errors["hr_channel_select"]     = "Select a confidential channel."
                ack(response_action="errors", errors=errors)
                return
        else:
            # Legacy selector blocks (original helper flow — kept for safety)
            pub_ch = values.get("block_public_channel", {}).get("public_channel_select", {}).get("selected_conversation") or ""
            hr_ch  = values.get("block_hr_channel",     {}).get("hr_channel_select",    {}).get("selected_conversation") or ""
            if not pub_ch or not hr_ch:
                ack(response_action="errors", errors={"block_public_channel": "Select a channel or enable auto-create.", "block_hr_channel": "Select a channel or enable auto-create."})
                return

    notion_state = secrets.token_hex(16)
    store_notion_state(notion_state, team_id)
    meta.update({"team_id": team_id, "auto_create": auto_create,
                 "public_channel": pub_ch, "hr_channel": hr_ch, "notion_state": notion_state})
    print(f"[wizard2] meta built: auto_create={auto_create} pub={pub_ch} hr={hr_ch} — pushing step 3")
    ack(response_action="push", view=wizard_step3(meta))

@app.view("wizard_step3")
def wizard3_submit(ack, body, view, client, logger):
    # MUST be first — closes the modal before Slack's 3s timeout
    ack({"response_action": "clear"})

    # Defer all work to background thread
    import threading

    def _do_finish():
        try:
            ui_errors = _wizard3_work(body, client)
            if ui_errors:
                # Modal is already closed — log errors, send DM to user instead
                user_id = body["user"]["id"]
                error_text = "\n".join(f"• {v}" for v in ui_errors.values())
                try:
                    client.chat_postMessage(
                        channel=user_id,
                        text=f"Setup could not complete:\n{error_text}\nPlease run /ha to try again."
                    )
                except Exception as dm_err:
                    logger.error(f"[wizard3] could not DM error to user: {dm_err}")
        except Exception as e:
            logger.error(f"[wizard3] background work failed: {type(e).__name__}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    threading.Thread(target=_do_finish, daemon=True).start()


def _wizard3_work(body, client):
    team_id = body["team"]["id"]
    user_id = body["user"]["id"]
    meta    = json.loads(body["view"].get("private_metadata", "{}"))
    values  = body["view"]["state"]["values"]
    print(f"[wizard3] submitted for {team_id} by {re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(user_id))} | "
          f"auto_create={meta.get('auto_create')} notion_state={meta.get('notion_state','')[:8]}")

    # ── Nuclear identity + scope check ──────────────────────────────────────
    try:
        auth = client.auth_test()
        scopes = auth.get("response_metadata", {}).get("scopes", [])
        print(f"[wizard3] auth_test OK — bot_id={auth.get('bot_id')} "
              f"user_id={auth.get('user_id')} team={auth.get('team_id')} "
              f"scopes={','.join(scopes) if scopes else auth.get('scope','N/A')[:300]}")
    except Exception as e:
        print(f"[wizard3] auth_test FAILED: {e}")

    try:
        existing = get_workspace_config(team_id)

        # ── Fast-exit idempotency ────────────────────────────────────────────
        if (existing
                and existing["public_channel"]
                and existing["hr_channel"]
                and existing["notion_api_key"]
                and existing["notion_database_id"]):
            print(f"[wizard3] FULLY CONFIGURED — idempotency exit, republishing home")
            publish_home(client, user_id, team_id)
            return

        pub_ch = meta.get("public_channel", "")
        hr_ch  = meta.get("hr_channel", "")

        if meta.get("auto_create"):
            if existing and existing["public_channel"] and existing["hr_channel"]:
                pub_ch = existing["public_channel"]
                hr_ch  = existing["hr_channel"]
                print(f"[wizard3] channels in DB — reusing: pub={pub_ch} hr={hr_ch}")
            else:
                print(f"[wizard3] auto-creating channels for {team_id}")
                pub_ch, pub_err = find_or_create_channel(client, "hush-public", is_private=False)
                hr_ch,  hr_err  = find_or_create_channel(client, "hush-hr",     is_private=True)
                print(f"[wizard3] channels result: pub={pub_ch} ({pub_err}) hr={hr_ch} ({hr_err})")

        # ── Gate: surface missing channels as modal errors ───────────────────
        ui_errors = {}
        if not pub_ch:
            err_msg = pub_err or "Could not create or find #hush-public."
            ui_errors["block_auto_create"] = f"📢 Public channel: {err_msg}"
        if not hr_ch:
            err_msg = hr_err or "Could not create or find #hush-hr."
            # Append to existing error or set new one
            existing_err = ui_errors.get("block_auto_create", "")
            ui_errors["block_auto_create"] = (existing_err + " | " if existing_err else "") + f"🔒 Private channel: {err_msg}"
        if ui_errors:
            print(f"[wizard3] ❌ UI errors: {ui_errors}")
            return ui_errors  # wizard3_submit will pass these to ack(errors=...)

        # ── Invite admin + HR leaders to HR channel ──────────────────────────
        # Only invite when we just auto-created channels (not on reuse or manual select)
        if meta.get("auto_create") and pub_ch and not (existing and existing["public_channel"]):
            try:
                client.conversations_invite(channel=pub_ch, users=user_id)
                print(f"[wizard3] invited admin to public channel {pub_ch}")
            except Exception as e:
                print(f"[wizard3] Failed to invite admin to public channel: {e}")

        if meta.get("auto_create") and hr_ch and not (existing and existing["hr_channel"]):
            try:
                client.conversations_invite(channel=hr_ch, users=user_id)
                print(f"[wizard3] invited admin to HR channel {hr_ch}")
            except Exception as e:
                print(f"[wizard3] Failed to invite admin to HR channel: {e}")

        hr_leaders = values.get("hr_leaders", {}).get("hr_leaders_input", {}).get("selected_users", [])
        if hr_leaders and hr_ch:
            try:
                client.conversations_invite(channel=hr_ch, users=",".join(hr_leaders))
                print(f"[wizard3] invited {len(hr_leaders)} HR leader(s) to {hr_ch}")
            except Exception as e:
                print(f"[wizard3] Failed to invite HR leaders: {e}")

        notion_key = existing["notion_api_key"]      if existing else None
        notion_db  = existing["notion_database_id"]  if existing else None
        if not NOTION_CLIENT_ID:
            manual_key = (values.get("block_notion_token", {}).get("notion_token_input", {}).get("value") or "").strip() or None
            manual_db  = (values.get("block_notion_db",    {}).get("notion_db_input",    {}).get("value") or "").strip() or None
            if manual_key: notion_key = manual_key
            if manual_db:  notion_db  = manual_db

        installer_id = existing["installer_id"] if (existing and existing["installer_id"]) else user_id
        save_workspace_config(team_id, installer_id, pub_ch, hr_ch, notion_key, notion_db)

    except Exception as e:
        print(f"[wizard3] ERROR for {team_id}: {e}")
        import traceback; traceback.print_exc()
        return {"block_auto_create": f"Setup failed: {e}"}

    try:
        publish_home(client, user_id, team_id)
        print(f"[wizard3] home published for {re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(user_id))}")
    except Exception as e:
        print(f"[wizard3] ERROR publishing home for {re.sub(r'U[A-Z0-9]{8,11}', '[REDACTED_USER]', str(user_id))}: {e}")

    return None  # success — ack() with no errors


# ── Example prompts ───────────────────────────────────────────────────────────

@app.action(re.compile(r"^example_(tech|feedback|idea)$"))
def handle_example(ack, body, client):
    ack()
    action_id = body["actions"][0]["action_id"]
    user_id   = body["user"]["id"]
    team_id   = body["team"]["id"]
    text      = EXAMPLE_MESSAGES.get(action_id, "")
    if not text: return
    dm_ch     = client.conversations_open(users=user_id)["channel"]["id"]
    user_hash = hash_user(user_id, team_id)
    token     = make_token(user_id, team_id)
    result    = client.chat_postMessage(channel=dm_ch, blocks=routing_blocks(token, text), text="Route your message:")
    save_pending(token, team_id, dm_ch, text, user_hash, result.get("ts"))


# ── Messaging ─────────────────────────────────────────────────────────────────

def handle_incoming(client, team_id, user_id, channel_id, text):
    if not text or not text.strip():
        client.chat_postMessage(channel=channel_id, text="Send a message to route it anonymously.")
        return
    clean = text.strip()
    if clean.startswith("<@"):
        parts = clean.split(">", 1)
        clean = parts[1].strip() if len(parts) > 1 else ""
    if not clean:
        client.chat_postMessage(channel=channel_id, text="Message is empty. Try again.")
        return
    user_hash = hash_user(user_id, team_id)
    token     = make_token(user_id, team_id)
    result    = client.chat_postMessage(channel=channel_id, blocks=routing_blocks(token, clean), text="Route your message:")
    save_pending(token, team_id, channel_id, clean, user_hash, result.get("ts"))

@app.message()
def on_dm(message, client, say):
    if message.get("channel_type") != "im":
        return
    if message.get("bot_id") or message.get("subtype"):
        return

    team_id = message["team"] if "team" in message else client.team_info()["team"]["id"]
    user_id = message["user"]
    user_hash = hash_user(user_id, team_id)
    text = message.get("text", "").strip()

    if not text:
        return

    # Escape hatch — user can force-close their active thread
    if text.lower().strip() in ("cancel", "end chat"):
        active = get_active_thread_for_user(team_id, user_hash)
        if active:
            close_thread(team_id, active["thread_ts"])
            try:
                client.chat_postMessage(
                    channel=message["channel"],
                    text="Previous conversation closed. You may now start a new one."
                )
            except Exception as e:
                logger.error(f"[escape] DM reply failed: {e}")
        else:
            try:
                client.chat_postMessage(
                    channel=message["channel"],
                    text="No active conversation to close. Send a message to start a new one."
                )
            except Exception as e:
                logger.error(f"[escape] DM reply failed: {e}")
        return  # Do not proceed to ingestion or 2-way routing

    # 2-Way Chat: Check if this user has an active triage thread
    active_thread = get_active_thread_for_user(team_id, user_hash)
    if active_thread:
        # Post anonymously back to the original triage thread
        thread_ts = active_thread["thread_ts"]
        target_channel = active_thread["target_channel"]
        try:
            client.chat_postMessage(
                channel=target_channel,
                thread_ts=thread_ts,
                text=f"💬 *Anonymous Sender:* {text}"
            )
            say("Your reply has been sent anonymously.")
        except Exception as e:
            logger.error(f"[2way] Failed to post anonymous reply: {e}")
            say("Unable to deliver your reply. Please try again.")
        return  # Do NOT fall through to new submission flow

    # No active thread — treat as new submission
    handle_incoming(client, team_id, user_id, message["channel"], text)

@app.event("app_mention")
def on_mention(event, client):
    handle_incoming(client, event["team"], event["user"], event["channel"], event.get("text",""))


def _deliver_reply_dm(client, source_channel: str, clean_reply: str, msg_id):
    """Actually deliver the DM and mark replied. Called directly or after confirm."""
    dm_text = f"A reply to your anonymous message:\n\n>{clean_reply}"
    client.chat_postMessage(
        channel=source_channel,
        text=dm_text,
        blocks=[
            {"type": "section", "text": {"type": "mrkdwn",
             "text": f"💬 *A reply to your anonymous message:*\n\n>{clean_reply}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": "🔒 Responder identity protected · HushAsk"}
            ]}
        ]
    )
    mark_replied_and_purge_source(msg_id)
    print(f"[reply_back] Delivered reply for msg_id={msg_id} to source_channel={source_channel}")


@app.message()
def on_triage_reply(message, client, body):
    """Anonymous reply-back listener.

    Fires when any message is posted. Filters to:
    - Thread replies only (has thread_ts != ts)
    - In a configured triage channel (public_channel or hr_channel) for this workspace
    - Not from a bot (no bot_id / subtype)

    Applies a safety filter: if the reply contains a workspace member's name,
    shows an ephemeral Block Kit prompt to the replier before delivering.
    Looks up the original submission by (channel, thread_ts) via Identity Vault,
    then DMs the reply back to the original sender with NO identity info.
    """
    # Ignore bot messages and system subtypes
    if message.get("bot_id") or message.get("subtype"):
        return

    # Must be a thread reply — thread_ts is set and differs from ts
    thread_ts = message.get("thread_ts")
    ts        = message.get("ts")
    if not thread_ts or thread_ts == ts:
        return  # top-level message — ignore

    channel    = message.get("channel")
    reply_text = (message.get("text") or "").strip()
    if not reply_text:
        return

    team_id = (body.get("team_id")
               or body.get("team", {}).get("id")
               or message.get("team", ""))

    # ── Triage channel scoping ────────────────────────────────────────────────
    # Only process replies in the configured triage channels for this workspace.
    try:
        ws_config = get_workspace_config(team_id)
    except Exception as e:
        print(f"[reply_back] Could not fetch workspace config for {team_id}: {e}")
        return

    if not ws_config:
        return  # Workspace not configured

    triage_channels = {ws_config.get("public_channel"), ws_config.get("hr_channel")} - {None, ""}
    if channel not in triage_channels:
        return  # Not a triage channel — ignore

    # ── Identity Vault lookup ─────────────────────────────────────────────────
    try:
        routing = get_routing(team_id, thread_ts)
        if routing:
            source_channel = routing["source_channel"]
            # Also fetch delivered record so we can call mark_replied
            delivered_record = get_delivered_by_thread_ts(channel, thread_ts)
            msg_id = delivered_record["id"] if delivered_record else None
        else:
            # Fallback: legacy delivered_messages lookup (for records before Identity Vault)
            record = get_delivered_by_thread_ts(channel, thread_ts)
            if not record:
                return  # Not a tracked triage thread
            source_channel = record["source_channel"]
            msg_id         = record["id"]

    except Exception as e:
        import traceback
        print(f"[reply_back] DB lookup error: {type(e).__name__}: {e}")
        print(traceback.format_exc())
        return

    if not source_channel:
        print(f"[reply_back] msg_id={msg_id} has no source_channel — cannot DM")
        return

    # Strip any Slack user/channel mentions from reply text for extra privacy
    clean_reply = re.sub(r"<@[A-Z0-9]+>", "[someone]", reply_text)
    clean_reply = re.sub(r"<#[A-Z0-9]+\|?[^>]*>", "[a channel]", clean_reply)

    # ── Safety Filter — Name Detection ────────────────────────────────────────
    replier_id = message.get("user", "")
    try:
        workspace_names = get_workspace_display_names(client, team_id)
        normalized_reply = normalize_for_name_check(clean_reply)
        words = [w for w in normalized_reply.split() if len(w) >= 3]
        name_hit = next((w for w in words if w in workspace_names), None)
    except Exception as e:
        name_hit = None

    if name_hit:
        # Possible name detected — ask the replier before delivering
        ctx = json.dumps({
            "source_channel": source_channel,
            "clean_reply":    clean_reply,
            "msg_id":         msg_id,
        })
        try:
            client.chat_postEphemeral(
                channel=channel,
                user=replier_id,
                text="⚠️ Identity Risk: Your reply may contain a name. Deliver anyway?",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": "⚠️ *Identity Risk*: Your reply may contain a name. Deliver anyway?"}},
                    {"type": "actions", "elements": [
                        {"type": "button", "action_id": "reply_deliver_confirm",
                         "style": "danger",
                         "text": {"type": "plain_text", "text": "Deliver", "emoji": True},
                         "value": ctx},
                        {"type": "button", "action_id": "reply_deliver_cancel",
                         "text": {"type": "plain_text", "text": "Cancel", "emoji": True},
                         "value": ctx},
                    ]},
                ]
            )
        except Exception as e:
            print(f"[reply_back] Failed to post safety ephemeral: {e}")
        return  # Hold delivery pending leader's choice

    # ── Deliver immediately ───────────────────────────────────────────────────
    try:
        _deliver_reply_dm(client, source_channel, clean_reply, msg_id)
    except Exception as e:
        print(f"[reply_back] Failed to DM reply for msg_id={msg_id}: {e}")


@app.action("reply_deliver_confirm")
def handle_reply_deliver_confirm(ack, body, client, respond):
    """Leader confirmed delivery despite name warning — send the DM."""
    ack()
    try:
        ctx = json.loads(body["actions"][0]["value"])
        source_channel = ctx["source_channel"]
        clean_reply    = ctx["clean_reply"]
        msg_id         = ctx.get("msg_id")
        _deliver_reply_dm(client, source_channel, clean_reply, msg_id)
        # Remove the ephemeral prompt via respond (chat_delete silently fails for ephemerals)
        respond(delete_original=True)
    except Exception as e:
        print(f"[reply_deliver_confirm] Error: {e}")


@app.action("reply_deliver_cancel")
def handle_reply_deliver_cancel(ack, body, client, respond):
    """Leader cancelled delivery — dismiss the ephemeral safety prompt."""
    ack()
    try:
        # Dismiss the safety prompt via respond (chat_delete silently fails for ephemerals)
        respond(delete_original=True)
    except Exception as e:
        print(f"[reply_deliver_cancel] Error: {e}")


@app.command("/ha")
def handle_ha_command(ack, body, client, logger):
    """Entry point for /ha slash command — DM-first UX.
    Redirects the user to their HushAsk DM with an anonymous message prompt.
    NOTE: /ha must be registered in the Slack app manifest by the workspace admin.
    """
    ack()  # Must ack within 3 seconds
    user_id = body["user_id"]
    try:
        result = client.conversations_open(users=user_id)
        dm_channel = result["channel"]["id"]
        client.chat_postMessage(
            channel=dm_channel,
            text="What would you like to share? Type your message and send it here.",
            blocks=[
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "What would you like to share?"
                    }
                },
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": "Type your message and send it here. Once received, you will select a route — public knowledge base or confidential HR."
                    }
                },
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": "🤫 Your identity is never stored or logged."
                        }
                    ]
                }
            ]
        )
    except Exception as e:
        logger.error(f"[/ha] failed to open DM: {e}")


# ── Routing actions ───────────────────────────────────────────────────────────

def _do_route(ack, body, client, route_type):
    ack()
    token   = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    # Atomic claim — concurrent button clicks get None and return early (TOCTOU fix)
    pending = claim_pending(token)
    if not pending: return

    team_id   = pending["team_id"]
    src       = pending["source_channel"]
    message   = pending["message"]
    user_hash = pending["user_hash"]
    msg_ts    = pending["message_ts"]

    allowed, usage = check_and_increment(team_id)
    if not allowed:
        try:   client.chat_update(channel=src, ts=msg_ts, blocks=limit_blocks(usage, team_id), text="Limit reached.")
        except: client.chat_postEphemeral(channel=src, user=user_id, blocks=limit_blocks(usage, team_id), text="Limit reached.")
        # Admin alert — DM the installer once when the workspace first hits the cap
        if usage == FREE_LIMIT:
            _alert_installer_limit(client, team_id, usage)
        return

    config     = get_workspace_config(team_id)
    has_notion  = bool(config and config["notion_api_key"] and config["notion_database_id"])
    show_notion = has_notion and (route_type == "public")  # HR never shows Notion button

    if route_type == "public":
        target = config["public_channel"] if (config and config["public_channel"]) else src
        label  = "📢 *Anonymous message — Public:*"
        conf   = "Public"
    else:
        target = config["hr_channel"] if (config and config["hr_channel"]) else src
        label  = "🔒 *Anonymous message — Private / HR:*"
        conf   = "Confidential / HR"

    try:
        # Post to triage channel first to capture thread_ts
        triage_result = client.chat_postMessage(
            channel=target,
            blocks=triage_blocks(message, label, 0, show_notion),
            text="Anonymous message via HushAsk"
        )
        triage_ts = triage_result.get("ts")
        # Atomic transaction — delivered_messages insert
        with get_conn() as conn:
            cur = conn.execute("""
                INSERT INTO delivered_messages
                    (team_id, target_channel, route_type, message, user_hash, source_channel, thread_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (team_id, target, route_type, message, user_hash, src, triage_ts))
            msg_id = cur.lastrowid
        # Save routing OUTSIDE conn block — save_routing() manages its own connection
        if triage_ts:
            save_routing(team_id, triage_ts, user_hash, src)
            logger.info(f"[route] routing_table saved: team={team_id}, thread_ts={triage_ts}, src={src}")
        # Update the triage post with correct msg_id + close blocks
        if triage_ts:
            close_value = json.dumps({
                "user_hash": user_hash,
                "thread_ts": triage_ts,
                "team_id": team_id,
                "target_channel": target,
                "route_type": route_type,
                "msg_ts": triage_ts
            })
            if route_type == "public":
                close_blocks = [
                    {"type": "divider"},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "thread_close_sync",
                                "style": "primary",
                                "text": {"type": "plain_text", "text": "Close & Sync to Notion"},
                                "value": close_value
                            },
                            {
                                "type": "button",
                                "action_id": "thread_close_only",
                                "text": {"type": "plain_text", "text": "Close Only"},
                                "value": close_value
                            }
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "🤫 Closing this thread will notify the anonymous sender."}]
                    }
                ]
            else:  # hr
                close_blocks = [
                    {"type": "divider"},
                    {
                        "type": "actions",
                        "elements": [
                            {
                                "type": "button",
                                "action_id": "thread_close_only",
                                "text": {"type": "plain_text", "text": "Close Conversation"},
                                "value": close_value
                            }
                        ]
                    },
                    {
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": "🤫 Closing this thread will notify the anonymous sender."}]
                    }
                ]
            try:
                client.chat_update(
                    channel=target, ts=triage_ts,
                    blocks=triage_blocks(message, label, msg_id, show_notion) + close_blocks,
                    text="Anonymous message via HushAsk"
                )
            except Exception as upd_e:
                logger.error(f"[route] failed to append close block: {upd_e}")
        # pending already removed by claim_pending — no delete_pending needed
        if msg_ts:
            client.chat_update(channel=src, ts=msg_ts, blocks=confirmed_blocks(conf), text="Delivered.")
    except Exception as e:
        print(f"[route_{route_type}] error: {e}")

def _intercept_route(ack, body, client, logger, route_type: str):
    ack()
    token   = body["actions"][0]["value"]
    msg_ts  = body["message"]["ts"]
    channel = body["channel"]["id"]

    pending = peek_pending(token)
    if not pending:
        # Already claimed or expired — silently ignore
        logger.warning("[confirm] peek_pending returned None for token (already claimed?)")
        return

    message = pending.get("message", "")

    try:
        client.chat_update(
            channel=channel,
            ts=msg_ts,
            blocks=route_confirmation_blocks(token, route_type, message),
            text=f"Confirm routing to {route_type}"
        )
    except Exception as e:
        logger.error(f"[confirm] chat_update failed: {e}")


@app.action("route_public")
def handle_route_public_intercept(ack, body, client, logger):
    _intercept_route(ack, body, client, logger, "public")


@app.action("route_hr")
def handle_route_hr_intercept(ack, body, client, logger):
    _intercept_route(ack, body, client, logger, "hr")


@app.action("route_confirm")
def handle_route_confirm(ack, body, client, logger):
    ack()
    raw_value = body["actions"][0]["value"]
    # value is "{token}|{route_type}"
    parts = raw_value.rsplit("|", 1)  # rsplit to handle tokens that might contain |
    if len(parts) != 2:
        logger.error(f"[confirm] malformed route_confirm value: {raw_value!r}")
        return
    token, route_type = parts[0], parts[1]

    # Patch the action value back to the bare token so _do_route reads it correctly
    body["actions"][0]["value"] = token
    # Pass a no-op ack — we've already acked above; _do_route also calls ack()
    _do_route(ack=lambda *a, **kw: None, body=body, client=client, route_type=route_type)


@app.action("route_cancel")
def handle_route_cancel(ack, body, client, logger):
    ack()
    token   = body["actions"][0]["value"]
    msg_ts  = body["message"]["ts"]
    channel = body["channel"]["id"]

    pending = peek_pending(token)
    if not pending:
        logger.warning("[cancel] pending not found for token — message may have been routed already")
        try:
            client.chat_update(
                channel=channel,
                ts=msg_ts,
                blocks=[{"type": "section", "text": {"type": "mrkdwn", "text": "This message has already been routed."}}],
                text="Already routed."
            )
        except Exception as e:
            logger.error(f"[cancel] chat_update failed: {e}")
        return

    message = pending.get("message", "")

    try:
        client.chat_update(
            channel=channel,
            ts=msg_ts,
            blocks=routing_blocks(token, message),
            text="Route your message:"
        )
    except Exception as e:
        logger.error(f"[cancel] chat_update failed: {e}")


# ── Notion sync ───────────────────────────────────────────────────────────────

@app.action("sync_notion")
def handle_sync_notion(ack, body, client):
    ack()
    msg_id  = int(body["actions"][0]["value"])
    team_id = body["team"]["id"]
    channel = body["channel"]["id"]
    msg_ts  = body["message"]["ts"]
    user_id = body["user"]["id"]

    delivered = get_delivered(msg_id)
    config    = get_workspace_config(team_id)

    if not delivered:
        client.chat_postEphemeral(channel=channel, user=user_id, text="Message not found.")
        return

    # GUARD: Confidential/HR messages are explicitly excluded from Notion sync
    if delivered["route_type"] == "hr":
        try:
            client.chat_postEphemeral(
                channel=channel,
                user=user_id,
                text="Notion sync is not available for confidential messages."
            )
        except Exception:
            pass
        return

    if delivered["notion_synced"]:
        client.chat_postEphemeral(channel=channel, user=user_id, text="Already synced.")
        return
    if not config or not config["notion_api_key"] or not config["notion_database_id"]:
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"Notion not configured. <{HELP_BASE}/setting-up-notion.html|Setup guide>")
        return

    ok, err = push_to_notion(config["notion_api_key"], config["notion_database_id"], delivered["message"], delivered["route_type"])
    if ok:
        mark_notion_synced(msg_id)
        try:
            new_blocks = [{"type":"section","text":{"type":"mrkdwn","text":body["message"]["blocks"][0]["text"]["text"]}},
                          {"type":"context","elements":[{"type":"mrkdwn","text":"✅ Synced · 🔒 Anonymous"}]}]
            client.chat_update(channel=channel, ts=msg_ts, blocks=new_blocks, text="Synced.")
        except: pass
    else:
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"⚠️ Notion sync failed: {err}")


# ── Thread Closure (Phase 6.6) ────────────────────────────────────────────────

def _get_source_channel(team_id: str, thread_ts: str, target_channel: str = None) -> str | None:
    """Return the DM channel for the anonymous user associated with a triage thread.

    Checks routing_table first (preferred). Falls back to delivered_messages if
    routing_table.source_channel was NULLed by the post-delivery purge.
    """
    routing = get_routing(team_id, thread_ts)
    if routing and routing.get("source_channel"):
        return routing["source_channel"]
    # Fallback — delivered_messages still holds source_channel for unread threads
    if target_channel:
        record = get_delivered_by_thread_ts(target_channel, thread_ts)
        if record and record.get("source_channel"):
            return record["source_channel"]
    return None


def _sync_thread_to_notion(client, team_id: str, channel: str, thread_ts: str, logger) -> None:
    """Fetch the full triage thread and push Q&A to Notion.

    Reuses push_to_notion() — the single source of Notion API logic.
    The original anonymous submission is the question; non-bot replies form the answer.
    """
    config = get_workspace_config(team_id)
    if not config or not config.get("notion_database_id") or not config.get("notion_api_key"):
        logger.warning(f"[close] no Notion config for team {team_id}, skipping sync")
        return

    try:
        result = client.conversations_replies(channel=channel, ts=thread_ts)
        messages = result.get("messages", [])
    except Exception as e:
        logger.error(f"[close] conversations_replies failed: {e}")
        return

    if not messages:
        return

    question = messages[0].get("text", "")
    replies  = [m.get("text", "") for m in messages[1:] if not m.get("bot_id")]
    answer   = "\n".join(replies) if replies else "(no reply)"

    # Compose a single text block for Notion: reuse push_to_notion (no duplication)
    combined = f"Q: {question}\n\nA: {answer}"
    ok, err = push_to_notion(config["notion_api_key"], config["notion_database_id"], combined, "public")
    if ok:
        logger.info(f"[close] Notion sync succeeded for thread {thread_ts}")
    else:
        logger.error(f"[close] Notion sync failed: {err}")


def _do_thread_close(body, client, logger, sync_notion: bool) -> None:
    """Core logic for both thread_close_only and thread_close_sync actions."""
    try:
        action       = body["actions"][0]
        data         = json.loads(action["value"])
        team_id      = data["team_id"]
        thread_ts    = data["thread_ts"]
        target_channel = data["target_channel"]
        route_type   = data["route_type"]
        msg_ts       = data["msg_ts"]
        user_hash    = data["user_hash"]  # noqa: F841 — reserved for audit logging
    except Exception as e:
        logger.error(f"[close] failed to parse action value: {e}")
        return

    # 1. Delete active thread from DB (kills 2-way routing)
    try:
        close_thread(team_id, thread_ts)
        logger.info(f"[close] thread {thread_ts} closed for team {team_id}")
    except Exception as e:
        logger.error(f"[close] DB close failed: {e}")

    # 2. DM the anonymous user
    try:
        source = _get_source_channel(team_id, thread_ts, target_channel)
        if source:
            client.chat_postMessage(
                channel=source,
                text="This conversation has been closed. Your next message will start a new conversation."
            )
        else:
            logger.warning(f"[close] no source_channel for thread {thread_ts} — DM skipped")
    except Exception as e:
        logger.error(f"[close] DM notification failed: {e}")

    # 3. Optionally sync to Notion (public route only)
    if sync_notion and route_type == "public":
        try:
            _sync_thread_to_notion(client, team_id, target_channel, thread_ts, logger)
        except Exception as e:
            logger.error(f"[close] Notion sync failed: {e}")

    # 4. Update the triage message — replace action blocks with closed state
    try:
        result = client.conversations_replies(
            channel=target_channel,
            ts=thread_ts,
            limit=1
        )
        original_msg = result["messages"][0] if result.get("messages") else None
        if original_msg:
            current_blocks = original_msg.get("blocks", [])
            # Strip trailing action blocks (close button section)
            clean_blocks = []
            for block in current_blocks:
                if block.get("type") == "actions":
                    break  # stop at first actions block in the close section
                clean_blocks.append(block)
            # Remove trailing divider if present
            while clean_blocks and clean_blocks[-1].get("type") == "divider":
                clean_blocks.pop()
            # Append closed state
            clean_blocks += [
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "🔒 Conversation closed."}}
            ]
            client.chat_update(
                channel=target_channel,
                ts=msg_ts,
                blocks=clean_blocks,
                text="🔒 Conversation closed."
            )
    except Exception as e:
        logger.error(f"[close] message update failed: {e}")


@app.action("thread_close_only")
def handle_thread_close_only(ack, body, client, logger):
    ack()
    _do_thread_close(body, client, logger, sync_notion=False)


@app.action("thread_close_sync")
def handle_thread_close_sync(ack, body, client, logger):
    ack()
    _do_thread_close(body, client, logger, sync_notion=True)


# ── Bootstrap ─────────────────────────────────────────────────────────────────

def _bootstrap_from_env():
    """Seed OR refresh the workspaces table from SLACK_BOT_TOKEN on every startup.
    This ensures reinstalls (which generate a new bot_token) stay in sync with the DB.
    If the token in the env matches what's stored, this is a cheap no-op."""
    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not bot_token:
        return
    try:
        from slack_sdk import WebClient as _WC
        client    = _WC(token=bot_token)
        auth      = client.auth_test()
        team_id   = auth["team_id"]
        team_name = auth.get("team", "")
        bot_user_id = auth.get("bot_id", "")
        app_id      = auth.get("app_id", os.environ.get("SLACK_APP_ID", ""))
        existing_token = find_bot_token(None, team_id)
        if existing_token == bot_token:
            print(f"[bootstrap] Workspace {team_id} token unchanged — skipping.")
            return
        # Token is new or missing — upsert it
        save_workspace(
            team_id=team_id,
            enterprise_id="",
            team_name=team_name,
            bot_token=bot_token,
            bot_user_id=bot_user_id,
            app_id=app_id,
            installer_user_id=None,
        )
        action = "Updated" if existing_token else "Seeded"
        print(f"[bootstrap] {action} workspace {team_id} ({team_name}) from SLACK_BOT_TOKEN.")
    except Exception as e:
        print(f"[bootstrap] Warning: could not sync workspace from env — {e}")


# ── Init ──────────────────────────────────────────────────────────────────────

init_db()
_bootstrap_from_env()
print("[HushAsk] App initialized (HTTP mode).")
