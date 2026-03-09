"""
app.py — HushAsk Slack bot
HTTP Events API mode (multi-tenant, Railway-hosted)
- SQLite-backed InstallationStore + OAuthStateStore
- 3-step setup wizard (conditional UI via views_update)
- Non-admin welcome screen with clickable examples
- Notion OAuth
- Freemium 20 msg/month cap (bypassed for Pro)
"""

import os, json, hashlib, secrets, time, re
import urllib.parse
import requests as http
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from slack_bolt import App
from slack_bolt.oauth.oauth_settings import OAuthSettings
from slack_sdk.oauth.installation_store import InstallationStore
from slack_sdk.oauth.installation_store.models.installation import Installation
from slack_sdk.oauth.state_store import OAuthStateStore

from database import (
    init_db,
    save_workspace, find_bot_token, is_workspace_pro,
    issue_slack_state, consume_slack_state,
    save_pending, get_pending, delete_pending,
    log_delivered, get_delivered, mark_notion_synced,
    get_workspace_config, save_workspace_config, reset_workspace_config,
    save_workspace_notion, store_notion_state, get_team_from_state, delete_notion_state,
    check_and_increment, get_usage,
)

# ── Config ────────────────────────────────────────────────────────────────────

HASH_SALT        = os.environ.get("HASH_SALT", "hushask-v1-salt")
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
            "groups:read", "groups:write",
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
    return hashlib.sha256(f"{HASH_SALT}:{team_id}:{user_id}".encode()).hexdigest()[:16]

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
    """Return the channel ID for `name`, creating it if needed.
    Logs every API call result explicitly so failures are visible in Railway.
    Returns None if the channel cannot be found or created.
    """
    ctype = "private_channel" if is_private else "public_channel"

    # ── Fast path: scan existing channels first ─────────────────────────────
    try:
        cursor = None
        while True:
            resp = client.conversations_list(
                types=ctype, limit=200, exclude_archived=True, cursor=cursor
            )
            if not resp.get("ok"):
                print(f"[channels] conversations_list FAILED for '{name}': {resp.get('error')}")
                break
            match = next((c for c in resp.get("channels", []) if c["name"] == name), None)
            if match:
                print(f"[channels] ✅ '{name}' exists: {match['id']} (type={ctype})")
                return match["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                print(f"[channels] '{name}' not found in existing channels — will create")
                break
    except Exception as e:
        print(f"[channels] conversations_list exception for '{name}': {e}")

    # ── Create the channel ───────────────────────────────────────────────────
    try:
        result = client.conversations_create(name=name, is_private=is_private)
        if result.get("ok"):
            ch_id = result["channel"]["id"]
            print(f"[channels] ✅ created '{name}': {ch_id} (private={is_private})")
            return ch_id
        else:
            error = result.get("error", "unknown")
            print(f"[channels] conversations_create FAILED for '{name}': error={error}")
            if error == "name_taken":
                # Shouldn't happen after list scan — try one final lookup
                print(f"[channels] name_taken race — scanning once more for '{name}'")
                try:
                    resp2 = client.conversations_list(types=ctype, limit=200, exclude_archived=True)
                    match = next((c for c in resp2.get("channels", []) if c["name"] == name), None)
                    if match:
                        print(f"[channels] ✅ found on re-scan: {match['id']}")
                        return match["id"]
                except Exception as e2:
                    print(f"[channels] re-scan exception: {e2}")
            elif error in ("missing_scope", "not_allowed_token_type", "restricted_action"):
                print(f"[channels] ❌ PERMISSION ERROR for '{name}': {error} — check bot scopes")
            return None
    except Exception as e:
        print(f"[channels] conversations_create exception for '{name}': {e}")
        return None

def upgrade_link(team_id):
    return f"{API_BASE}/upgrade?team_id={team_id}"


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
    try:
        r = http.post("https://api.notion.com/v1/pages", json=payload, headers=headers, timeout=10)
        return (True, "") if r.status_code == 200 else (False, r.json().get("message", f"HTTP {r.status_code}"))
    except Exception as e:
        return False, str(e)


# ── Block builders ────────────────────────────────────────────────────────────

EXAMPLE_MESSAGES = {
    "example_tech":     "Our deploy process feels fragile — has anyone proposed a more reliable approach?",
    "example_feedback": "I'd like to discuss my compensation but I'm not sure who to talk to or how to start.",
    "example_idea":     "What if we ran a quarterly retrospective open to every team, not just engineering?",
}

def routing_blocks(token, message):
    preview = message if len(message) <= 280 else message[:277] + "…"
    return [
        {"type":"section","text":{"type":"mrkdwn","text":"🔒 *Your identity has been anonymized.* Choose how to route your message:"}},
        {"type":"section","text":{"type":"mrkdwn","text":f"*Your message:*\n>{preview}"}},
        {"type":"divider"},
        {"type":"actions","elements":[
            {"type":"button","action_id":"route_public","style":"primary","text":{"type":"plain_text","text":"📢 Public","emoji":True},"value":token},
            {"type":"button","action_id":"route_hr","style":"danger","text":{"type":"plain_text","text":"🔒 Private / HR","emoji":True},"value":token},
        ]},
        {"type":"context","elements":[{"type":"mrkdwn","text":"Your Slack identity will never be stored or shared."}]}
    ]

def confirmed_blocks(label):
    return [{"type":"section","text":{"type":"mrkdwn","text":f"✅ *Delivered anonymously.*\nRouted to: *{label}*"}}]

def triage_blocks(message, label, msg_id, has_notion):
    blocks = [{"type":"section","text":{"type":"mrkdwn","text":f"{label}\n\n{message}"}}]
    if has_notion:
        blocks.append({"type":"actions","elements":[{"type":"button","action_id":"sync_notion","text":{"type":"plain_text","text":"📄 Sync to Notion","emoji":True},"value":str(msg_id)}]})
    blocks.append({"type":"context","elements":[{"type":"mrkdwn","text":"🔒 Delivered anonymously via HushAsk"}]})
    return blocks

def limit_blocks(usage, team_id=""):
    url = upgrade_link(team_id) if team_id else UPGRADE_URL
    return [
        {"type":"section","text":{"type":"mrkdwn","text":f"⚠️ *You've hit the free tier limit.*\nYour workspace has sent *{usage}/{FREE_LIMIT}* anonymous messages this month.\n\nUpgrade to Pro for unlimited routing, priority support, and advanced analytics."}},
        {"type":"actions","elements":[{"type":"button","action_id":"upgrade_click","style":"primary","text":{"type":"plain_text","text":"🚀 Upgrade to Pro","emoji":True},"url":url}]},
        {"type":"context","elements":[{"type":"mrkdwn","text":"Resets automatically at the start of your next billing month."}]}
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
            text=f"⚠️ Your workspace has hit the HushAsk free tier limit ({usage}/{FREE_LIMIT} messages this month).",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    f"⚠️ *HushAsk free tier limit reached.*\n\n"
                    f"Your workspace has sent *{usage}/{FREE_LIMIT}* anonymous messages this month. "
                    f"New submissions are paused until the month resets or you upgrade.\n\n"
                    f"Upgrade to Pro for unlimited routing, priority support, and Notion Vault sync."
                }},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "⭐ Upgrade to Pro"},
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
        {"type":"header","text":{"type":"plain_text","text":"🎉 Welcome to HushAsk Pro!","emoji":True}},
        {"type":"section","text":{"type":"mrkdwn","text":"Your workspace is now on Pro. Here's what you've unlocked:\n\n✅ *Unlimited anonymous messages* — no monthly cap\n✅ *Priority support*\n✅ *Full Notion Vault sync*\n✅ *Multi-channel routing*"}},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":"Your team can keep sending — we handle the rest. 🔒"}},
        {"type":"context","elements":[{"type":"mrkdwn","text":"Questions? Email hello@hushask.com"}]}
    ]


# ── App Home views ────────────────────────────────────────────────────────────

def home_welcome():
    return {
        "type":"home","blocks":[
            {"type":"header","text":{"type":"plain_text","text":"Welcome to HushAsk 👋","emoji":True}},
            {"type":"section","text":{"type":"mrkdwn","text":"_Turning transient chat into a permanent library — anonymously._"}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"*Your voice, protected.* Send any question, idea, or concern to the right channel — anonymously. Your identity is hashed and never stored."}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"*Try an example — click to send it through the bot:*"}},
            {"type":"section","text":{"type":"mrkdwn","text":"💻 *Ask a Tech Question*\n_\"Our deploy process feels fragile — has anyone proposed a more reliable approach?\"_"},"accessory":{"type":"button","action_id":"example_tech","text":{"type":"plain_text","text":"Try this →","emoji":True},"style":"primary"}},
            {"type":"section","text":{"type":"mrkdwn","text":"🧑‍💼 *Send HR Feedback*\n_\"I'd like to discuss my compensation but I'm not sure who to talk to or how to start.\"_"},"accessory":{"type":"button","action_id":"example_feedback","text":{"type":"plain_text","text":"Try this →","emoji":True}}},
            {"type":"section","text":{"type":"mrkdwn","text":"💡 *Share a Company Idea*\n_\"What if we ran a quarterly retrospective open to every team, not just engineering?\"_"},"accessory":{"type":"button","action_id":"example_idea","text":{"type":"plain_text","text":"Try this →","emoji":True}}},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":f"🔒 Your Slack ID is SHA-256 hashed before any data is stored. · <{HELP_BASE}/privacy-and-hashing.html|Learn more>"}]}
        ]
    }

def home_unconfigured():
    return {
        "type":"home","blocks":[
            {"type":"header","text":{"type":"plain_text","text":"HushAsk Command Center ⚙️","emoji":True}},
            {"type":"section","text":{"type":"mrkdwn","text":"_Anonymous Slack router · by HonestAlias_"}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"👋 *Welcome, admin.* HushAsk isn't configured yet.\n\nRun the 3-step wizard to set up routing channels and optionally connect Notion."}},
            {"type":"actions","elements":[{"type":"button","action_id":"start_setup","style":"primary","text":{"type":"plain_text","text":"⚙️ Start Setup Wizard","emoji":True}}]},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":"🔒 Only workspace admins and the installer can access this panel."}]}
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
        {"type":"header","text":{"type":"plain_text","text":"HushAsk Command Center ⚙️","emoji":True}},
        {"type":"section","text":{"type":"mrkdwn","text":"_Anonymous Slack router · by HonestAlias_"}},
        {"type":"divider"},
        {"type":"section","text":{"type":"mrkdwn","text":"*Current Configuration*"}},
        {"type":"section","fields":[
            {"type":"mrkdwn","text":f"*📢 Public Channel*\n{pub}"},
            {"type":"mrkdwn","text":f"*🔒 Private Channel*\n{hr}"},
            {"type":"mrkdwn","text":f"*📄 Notion Vault*\n{notion}"},
            {"type":"mrkdwn","text":f"*📊 Usage*\n{tier}"},
        ]},
    ]

    if not pro:
        blocks.append({
            "type":"section",
            "text":{"type":"mrkdwn","text":f"*Monthly usage* {bar}  {pct}%"}
        })

    buttons = [
        {"type":"button","action_id":"edit_settings","style":"primary","text":{"type":"plain_text","text":"✏️ Edit Settings","emoji":True}},
        {"type":"button","action_id":"reset_config","style":"danger","text":{"type":"plain_text","text":"🔄 Reset","emoji":True},"confirm":{"title":{"type":"plain_text","text":"Reset configuration?"},"text":{"type":"mrkdwn","text":"Clears routing and Notion settings. History preserved."},"confirm":{"type":"plain_text","text":"Yes, reset"},"deny":{"type":"plain_text","text":"Cancel"},"style":"danger"}},
    ]
    if not pro:
        buttons.append({"type":"button","action_id":"upgrade_click","text":{"type":"plain_text","text":"🚀 Upgrade to Pro","emoji":True},"url":upgrade_link(team_id),"style":"primary"})

    blocks += [
        {"type":"actions","elements":buttons},
        {"type":"divider"},
        {"type":"context","elements":[
            {"type":"mrkdwn","text":f"<{HELP_BASE}/|Help Center> · {'⭐ Pro Plan' if pro else 'Free Plan'} · `Build: {BUILD_ID}`"}
        ]}
    ]
    return {"type":"home","blocks":blocks}

BUILD_ID = "540ca15"  # git short SHA — update on each deploy for UI verification

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

    print(f"[publish_home] build={BUILD_ID} user={user_id} team={team_id} db={DB_PATH} | "
          f"configured={is_configured} pub={pub_ch} hr={hr_ch} "
          f"notion_key={bool(notion_key)} notion_db={bool(notion_db)} installer={installer_id}")

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
        "title":{"type":"plain_text","text":"HushAsk Setup (1/3)"},
        "submit":{"type":"plain_text","text":"Get Started →"},
        "close":{"type":"plain_text","text":"Cancel"},
        "blocks":[
            {"type":"header","text":{"type":"plain_text","text":"Turning transient chat into a permanent library.","emoji":True}},
            {"type":"divider"},
            {"type":"section","text":{"type":"mrkdwn","text":"HushAsk gives your team a safe, anonymous way to speak up — and turns every answered question into lasting company knowledge.\n\n*Here's the full sequence:*"}},
            {"type":"section","fields":[
                {"type":"mrkdwn","text":"*1️⃣  DM the bot*\nAnyone sends a message directly to HushAsk."},
                {"type":"mrkdwn","text":"*2️⃣  Choose a route*\n📢 Public Knowledge or 🔒 Private / HR."},
                {"type":"mrkdwn","text":"*3️⃣  Team response*\nThe right people see it in the right channel."},
                {"type":"mrkdwn","text":"*4️⃣  Sync to Notion _(optional)_*\nOne click turns the Q&A into a permanent doc."},
            ]},
            {"type":"divider"},
            {"type":"context","elements":[{"type":"mrkdwn","text":"The Notion step is completely optional. This wizard takes about 2 minutes."}]}
        ]
    }

def wizard_step2_modal(auto_create=True, meta=None):
    if meta is None: meta = {}
    auto_el = {
        "type":"checkboxes","action_id":"auto_create_check",
        "options":[{"text":{"type":"mrkdwn","text":"*Create channels for me*\nSpins up `#hush-public` (📢 Public) and `#hush-hr` (🔒 Private)."},"value":"auto_create"}],
    }
    if auto_create:
        auto_el["initial_options"] = [{"text":{"type":"mrkdwn","text":"*Create channels for me*\nSpins up `#hush-public` (📢 Public) and `#hush-hr` (🔒 Private)."},"value":"auto_create"}]

    blocks = [
        {"type":"header","text":{"type":"plain_text","text":"Infrastructure — Triage Channels"}},
        {"type":"section","text":{"type":"mrkdwn","text":"HushAsk routes messages to two channels: one *Public* and one *Private* (confidential / HR)."}},
        {"type":"divider"},
        {"type":"input","block_id":"block_auto_create","label":{"type":"plain_text","text":"🔧 Channel setup"},"optional":True,"element":auto_el},
    ]
    if auto_create:
        blocks.append({"type":"section","text":{"type":"mrkdwn","text":"✅ *We'll create both channels automatically.*\n\n_`#hush-public` (📢 Public) and `#hush-hr` (🔒 Private) will be created if they don't already exist._\n\n_Uncheck to pick existing channels instead._"}})
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
            {"type": "section", "text": {"type": "mrkdwn", "text": "⚠️ *Bot access required:* The bot must be a member of both channels. Invite it with `/invite @HushAsk` after setup."}},
            {"type": "input", "block_id": "block_public_channel",
             "label": {"type": "plain_text", "text": "📢 Public Channel"},
             "hint":  {"type": "plain_text", "text": "Anonymous public messages route here."},
             "optional": False, "element": pub_el},
            {"type": "input", "block_id": "block_hr_channel",
             "label": {"type": "plain_text", "text": "🔒 Private / HR Channel"},
             "hint":  {"type": "plain_text", "text": "Confidential messages. Invite the bot first."},
             "optional": False, "element": hr_el},
        ]
    return {
        "type":"modal","callback_id":"wizard_step2",
        "private_metadata":json.dumps(meta),
        "title":{"type":"plain_text","text":"HushAsk Setup (2/3)"},
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
            {"type":"section","text":{"type":"mrkdwn","text":f"Click below to authorize HushAsk in your Notion workspace. We'll automatically create a *Hush Library* database — no tokens or page IDs needed. <{HELP_BASE}/setting-up-notion.html|Setup guide →>"}},
            {"type":"divider"},
            {"type":"actions","elements":[{"type":"button","action_id":"notion_oauth_click","style":"primary","text":{"type":"plain_text","text":"🔗 Connect to Notion","emoji":True},"url":oauth_url}]},
            {"type":"context","elements":[{"type":"mrkdwn","text":"After connecting in your browser, return here and click *Save & Finish*."}]}
        ]
    else:
        vault_blocks = [
            {"type":"section","text":{"type":"mrkdwn","text":f"Provide your Notion token and Hush Library database ID. <{HELP_BASE}/setting-up-notion.html|Setup guide →>"}},
            {"type":"divider"},
            {"type":"input","block_id":"block_notion_token","label":{"type":"plain_text","text":"Notion API Token"},"optional":True,"element":{"type":"plain_text_input","action_id":"notion_token_input","placeholder":{"type":"plain_text","text":"secret_..."},"initial_value":meta.get("notion_api_key","")}},
            {"type":"input","block_id":"block_notion_db","label":{"type":"plain_text","text":"Hush Library Database ID"},"optional":True,"element":{"type":"plain_text_input","action_id":"notion_db_input","placeholder":{"type":"plain_text","text":"32-char database ID"},"initial_value":meta.get("notion_database_id","")}},
            {"type":"context","elements":[{"type":"mrkdwn","text":"Both fields optional — add Notion later from Settings."}]}
        ]
    return {
        "type":"modal","callback_id":"wizard_step3",
        "private_metadata":json.dumps(meta),
        "title":{"type":"plain_text","text":"HushAsk Setup (3/3)"},
        "submit":{"type":"plain_text","text":"Save & Finish ✓"},
        "close":{"type":"plain_text","text":"Back"},
        "blocks":[{"type":"header","text":{"type":"plain_text","text":"The Notion Vault — Optional","emoji":True}},*vault_blocks]
    }


# ── Events & Actions ──────────────────────────────────────────────────────────

@app.event("app_home_opened")
def handle_home_opened(event, client, body):
    user_id = event["user"]
    # Skip the Messages tab — only publish on the Home tab
    if event.get("tab") == "messages":
        return
    # team_id is always in the outer body, never rely on event["view"]
    team_id = body.get("team_id", "")
    if not team_id:
        print(f"[home] WARNING: no team_id in body for user {user_id}")
        return
    print(f"[home] publishing for user={user_id} team={team_id}")
    publish_home(client, user_id, team_id)
    _maybe_send_install_nudge(client, user_id, team_id)


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
            text="Welcome to HushAsk! Run /ha to get started.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "👋 *Welcome to HushAsk!*\n\n"
                    "Anonymous feedback for your team — set up takes 60 seconds.\n\n"
                    "Run `/ha` in any channel to open the setup wizard and configure "
                    "your public and private routing channels."
                }},
                {"type": "actions", "elements": [
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "Open App Home"},
                     "action_id": "open_home_nudge",
                     "style": "primary",
                     "url": f"slack://app?team={team_id}&id={os.environ.get('SLACK_APP_ID', '')}"}
                ]},
                {"type": "context", "elements": [
                    {"type": "mrkdwn",
                     "text": f"Need help? Visit <{HELP_BASE}|hushask.com/help>"}
                ]}
            ]
        )
        mark_nudge_sent(team_id)
        print(f"[install_nudge] Sent to {user_id} for workspace {team_id}")
    except Exception as e:
        print(f"[install_nudge] error: {e}")

def _open_wizard(ack, body, client):
    ack()
    team_id = body["team"]["id"]
    config  = get_workspace_config(team_id)
    meta = {}
    if config:
        meta = {"public_channel": config["public_channel"] or "", "hr_channel": config["hr_channel"] or "",
                "notion_api_key": config["notion_api_key"] or "", "notion_database_id": config["notion_database_id"] or ""}
    client.views_open(trigger_id=body["trigger_id"], view=wizard_step1())

app.action("start_setup")(_open_wizard)
app.action("edit_settings")(_open_wizard)

@app.action("reset_config")
def handle_reset(ack, body, client):
    ack()
    reset_workspace_config(body["team"]["id"])
    publish_home(client, body["user"]["id"], body["team"]["id"])

@app.action("auto_create_check")
def handle_auto_toggle(ack, body, client):
    ack()
    selected    = body["actions"][0].get("selected_options", [])
    auto_create = any(o["value"] == "auto_create" for o in selected)
    meta        = json.loads(body["view"].get("private_metadata", "{}"))
    client.views_update(view_id=body["view"]["id"], view=wizard_step2_modal(auto_create=auto_create, meta=meta))

@app.action("notion_oauth_click")
def handle_notion_oauth_click(ack): ack()

@app.action("upgrade_click")
def handle_upgrade(ack): ack()

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
def wizard3_submit(ack, body, client):
    """ACK Slack immediately, then do ALL work in a daemon thread.
    This guarantees the HTTP 200 reaches Slack in <100ms regardless of
    how long channel creation or DB writes take — kills the retry loop."""
    ack()
    import threading
    threading.Thread(target=_wizard3_work, args=(body, client), daemon=True).start()


def _wizard3_work(body, client):
    team_id = body["team"]["id"]
    user_id = body["user"]["id"]
    meta    = json.loads(body["view"].get("private_metadata", "{}"))
    values  = body["view"]["state"]["values"]
    print(f"[wizard3] started background work for {team_id} by {user_id} | "
          f"auto_create={meta.get('auto_create')} notion_state={meta.get('notion_state','')[:8]}")

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
                pub_id = find_or_create_channel(client, "hush-public", is_private=False)
                hr_id  = find_or_create_channel(client, "hush-hr",     is_private=True)
                print(f"[wizard3] channels result: pub={pub_id} hr={hr_id}")
                if pub_id: pub_ch = pub_id
                if hr_id:  hr_ch  = hr_id

        # ── Gate: only commit if BOTH channels resolved ──────────────────────
        if not pub_ch or not hr_ch:
            print(f"[wizard3] ❌ ABORT — channel IDs missing: pub={pub_ch!r} hr={hr_ch!r}")
            publish_home(client, user_id, team_id)
            return

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

    try:
        publish_home(client, user_id, team_id)
        print(f"[wizard3] home published for {user_id}")
    except Exception as e:
        print(f"[wizard3] ERROR publishing home for {user_id}: {e}")
        import traceback; traceback.print_exc()


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
        client.chat_postMessage(channel=channel_id, text="Send me a message and I'll route it anonymously. 🔒")
        return
    clean = text.strip()
    if clean.startswith("<@"):
        parts = clean.split(">", 1)
        clean = parts[1].strip() if len(parts) > 1 else ""
    if not clean:
        client.chat_postMessage(channel=channel_id, text="What would you like to say anonymously? 🔒")
        return
    user_hash = hash_user(user_id, team_id)
    token     = make_token(user_id, team_id)
    result    = client.chat_postMessage(channel=channel_id, blocks=routing_blocks(token, clean), text="Route your message:")
    save_pending(token, team_id, channel_id, clean, user_hash, result.get("ts"))

@app.message()
def on_dm(message, client):
    if message.get("channel_type") != "im": return
    if message.get("bot_id") or message.get("subtype"): return
    handle_incoming(client, message["team"], message["user"], message["channel"], message.get("text",""))

@app.event("app_mention")
def on_mention(event, client):
    handle_incoming(client, event["team"], event["user"], event["channel"], event.get("text",""))


@app.command("/ha")
def handle_ha_command(ack, body, client):
    """Entry point for /ha slash command.
    - No text: open the setup wizard (admin) or show routing prompt (user)
    - With text: route the text as an anonymous message
    """
    ack()
    user_id = body["user_id"]
    team_id = body["team_id"]
    text    = (body.get("text") or "").strip()
    print(f"[/ha] user={user_id} team={team_id} text={repr(text)}")

    config      = get_workspace_config(team_id)
    installer_id = config["installer_id"] if config else None

    if not text:
        # No text — open wizard for admin/installer, routing hint for everyone else
        if is_admin(client, user_id) or user_id == installer_id:
            _open_wizard(ack=lambda **_: None, body=body, client=client)
        else:
            dm = client.conversations_open(users=user_id)["channel"]["id"]
            client.chat_postMessage(
                channel=dm,
                text="Send me a message here and I'll route it anonymously. 🔒",
                blocks=[
                    {"type": "section", "text": {"type": "mrkdwn",
                     "text": "👋 DM me directly with your message and I'll route it anonymously.\n\nOr type `/ha your message here` to send right now."}},
                ]
            )
    else:
        # Text provided — route it as an anonymous message
        dm = client.conversations_open(users=user_id)["channel"]["id"]
        handle_incoming(client, team_id, user_id, dm, text)


# ── Routing actions ───────────────────────────────────────────────────────────

def _do_route(ack, body, client, route_type):
    ack()
    token   = body["actions"][0]["value"]
    user_id = body["user"]["id"]
    pending = get_pending(token)
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
    has_notion = bool(config and config["notion_api_key"] and config["notion_database_id"])

    if route_type == "public":
        target = config["public_channel"] if (config and config["public_channel"]) else src
        label  = "📢 *Anonymous message — Public:*"
        conf   = "📢 Public"
    else:
        target = config["hr_channel"] if (config and config["hr_channel"]) else src
        label  = "🔒 *Anonymous message — Private / HR:*"
        conf   = "🔒 Private / HR"

    try:
        msg_id = log_delivered(team_id, target, route_type, message, user_hash)
        client.chat_postMessage(channel=target, blocks=triage_blocks(message, label, msg_id, has_notion), text="Anonymous message via HushAsk")
        delete_pending(token)
        if msg_ts:
            client.chat_update(channel=src, ts=msg_ts, blocks=confirmed_blocks(conf), text="Delivered.")
    except Exception as e:
        print(f"[route_{route_type}] error: {e}")

@app.action("route_public")
def handle_public(ack, body, client): _do_route(ack, body, client, "public")

@app.action("route_hr")
def handle_hr(ack, body, client): _do_route(ack, body, client, "hr")


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
        client.chat_postEphemeral(channel=channel, user=user_id, text="⚠️ Message record not found.")
        return
    if delivered["notion_synced"]:
        client.chat_postEphemeral(channel=channel, user=user_id, text="✅ Already synced to Notion.")
        return
    if not config or not config["notion_api_key"] or not config["notion_database_id"]:
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"⚠️ Notion isn't configured. <{HELP_BASE}/setting-up-notion.html|Setup guide>")
        return

    ok, err = push_to_notion(config["notion_api_key"], config["notion_database_id"], delivered["message"], delivered["route_type"])
    if ok:
        mark_notion_synced(msg_id)
        try:
            new_blocks = [{"type":"section","text":{"type":"mrkdwn","text":body["message"]["blocks"][0]["text"]["text"]}},
                          {"type":"context","elements":[{"type":"mrkdwn","text":"✅ Synced to Notion · 🔒 Delivered anonymously via HushAsk"}]}]
            client.chat_update(channel=channel, ts=msg_ts, blocks=new_blocks, text="Synced.")
        except: pass
    else:
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"⚠️ Notion sync failed: {err}")


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
