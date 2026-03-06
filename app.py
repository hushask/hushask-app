"""
app.py — HushAsk Slack bot (Socket Mode)
- 3-step sequential wizard with true conditional UI (views_update on checkbox toggle)
- Non-admin welcome screen with clickable example prompts
- Notion OAuth or manual fallback
- 20 msg/month freemium cap with upgrade wall
"""

import os, json, hashlib, secrets, time, re
import urllib.parse
import requests as http
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from slack_bolt import App
from slack_bolt.adapter.socket_mode import SocketModeHandler
from database import (
    init_db,
    save_pending, get_pending, delete_pending,
    log_delivered, get_delivered, mark_notion_synced,
    get_workspace_config, save_workspace_config, reset_workspace_config,
    save_workspace_notion,
    store_notion_state, get_team_from_state,
    check_and_increment, get_usage,
)

# ── Config ────────────────────────────────────────────────────────────────────

HASH_SALT          = os.environ.get("HASH_SALT", "hushask-v1-salt")
FREE_LIMIT         = int(os.environ.get("FREE_LIMIT", "20"))
UPGRADE_URL        = os.environ.get("UPGRADE_URL", "http://178.128.28.93:8080/#early-access")
HELP_BASE          = os.environ.get("HELP_BASE", "http://178.128.28.93:8080/help")
NOTION_CLIENT_ID   = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_REDIRECT    = os.environ.get("NOTION_REDIRECT_URI", "http://178.128.28.93:8080/notion/callback")

app = App(
    token=os.environ["SLACK_BOT_TOKEN"],
    signing_secret=os.environ["SLACK_SIGNING_SECRET"]
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def hash_user(user_id, team_id):
    return hashlib.sha256(f"{HASH_SALT}:{team_id}:{user_id}".encode()).hexdigest()[:16]

def make_token(user_id, team_id):
    return hashlib.sha256(
        f"{user_id}:{team_id}:{time.time()}:{secrets.token_hex(8)}".encode()
    ).hexdigest()[:32]

def resolve_team_id(client):
    try:    return client.auth_test()["team_id"]
    except: return "unknown"

def is_admin(client, user_id):
    try:
        u = client.users_info(user=user_id)["user"]
        return bool(u.get("is_admin") or u.get("is_owner") or u.get("is_primary_owner"))
    except:
        return False

def channel_display(client, cid):
    if not cid: return "—"
    try:    return f"#{client.conversations_info(channel=cid)['channel']['name']}"
    except: return cid

def find_or_create_channel(client, name, is_private):
    try:
        return client.conversations_create(name=name, is_private=is_private)["channel"]["id"]
    except Exception as e:
        if "name_taken" in str(e):
            ctype = "private_channel" if is_private else "public_channel"
            try:
                chs = client.conversations_list(types=ctype, limit=200)["channels"]
                m = next((c for c in chs if c["name"] == name), None)
                return m["id"] if m else None
            except: return None
        raise


# ── Notion ────────────────────────────────────────────────────────────────────

def push_to_notion(token, database_id, message, route_type):
    """Create a row in the Hush Library Notion database."""
    label = "📢 Public" if route_type == "public" else "🔒 Confidential / HR"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    payload = {
        "parent": {"database_id": database_id},
        "properties": {
            "Name": {"title": [{"text": {"content": message[:80] + ("…" if len(message) > 80 else "")}}]},
            "Route": {"select": {"name": label}},
            "Status": {"select": {"name": "New"}},
            "Synced At": {"date": {"start": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")}},
        },
        "children": [
            {"object": "block", "type": "callout", "callout": {
                "rich_text": [{"type": "text", "text": {"content": f"Route: {label} · Sender identity protected 🔒"}}],
                "icon": {"emoji": "🔒"}, "color": "gray_background"
            }},
            {"object": "block", "type": "heading_2", "heading_2": {
                "rich_text": [{"type": "text", "text": {"content": "Anonymous Message"}}]
            }},
            {"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": message}}]
            }},
            {"object": "block", "type": "divider", "divider": {}},
            {"object": "block", "type": "paragraph", "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": "Add your answer below ↓"},
                               "annotations": {"italic": True, "color": "gray"}}]
            }},
        ]
    }
    try:
        r = http.post("https://api.notion.com/v1/pages", json=payload, headers=headers, timeout=10)
        return (True, "") if r.status_code == 200 else (False, r.json().get("message", f"HTTP {r.status_code}"))
    except Exception as e:
        return False, str(e)


# ── App Home views ────────────────────────────────────────────────────────────

EXAMPLE_MESSAGES = {
    "example_tech":     "Our deploy process feels fragile — has anyone proposed a more reliable approach?",
    "example_feedback": "I'd like to discuss my compensation but I'm not sure who to talk to or how to start.",
    "example_idea":     "What if we ran a quarterly retrospective open to every team, not just engineering?",
}

def home_welcome() -> dict:
    """Non-admin: Welcome screen with clickable example prompts."""
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "Welcome to HushAsk 👋", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Turning transient chat into a permanent library — anonymously._"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Your voice, protected.* Send any question, idea, or concern to the right channel — anonymously. Your identity is hashed and never stored."}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Try an example — click to send it through the bot:*"}},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "💻 *Ask a Tech Question*\n_\"Our deploy process feels fragile — has anyone proposed a more reliable approach?\"_"},
                "accessory": {
                    "type": "button", "action_id": "example_tech",
                    "text": {"type": "plain_text", "text": "Try this →", "emoji": True},
                    "style": "primary"
                }
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "🧑‍💼 *Send HR Feedback*\n_\"I'd like to discuss my compensation but I'm not sure who to talk to or how to start.\"_"},
                "accessory": {
                    "type": "button", "action_id": "example_feedback",
                    "text": {"type": "plain_text", "text": "Try this →", "emoji": True}
                }
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "💡 *Share a Company Idea*\n_\"What if we ran a quarterly retrospective open to every team, not just engineering?\"_"},
                "accessory": {
                    "type": "button", "action_id": "example_idea",
                    "text": {"type": "plain_text", "text": "Try this →", "emoji": True}
                }
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"🔒 Your Slack ID is SHA-256 hashed before any data is stored. · <{HELP_BASE}/privacy-and-hashing.html|Learn more>"}]
            }
        ]
    }


def home_unconfigured() -> dict:
    """Admin: workspace not yet configured."""
    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "HushAsk Command Center ⚙️", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Anonymous Slack router · by HonestAlias_"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "👋 *Welcome, admin.* HushAsk isn't configured yet.\n\nRun the 3-step wizard to set up routing channels and optionally connect your Notion workspace."}},
            {"type": "actions", "elements": [{
                "type": "button", "action_id": "start_setup",
                "text": {"type": "plain_text", "text": "⚙️ Start Setup Wizard", "emoji": True},
                "style": "primary"
            }]},
            {"type": "divider"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": "🔒 Only workspace admins and the installer can access this panel."}]}
        ]
    }


def home_configured(config, client, team_id) -> dict:
    """Admin: live dashboard."""
    pub    = channel_display(client, config["public_channel"])
    hr     = channel_display(client, config["hr_channel"])
    n_key  = config["notion_api_key"]
    n_db   = config["notion_database_id"]
    notion = "✅ Hush Library connected" if (n_key and n_db) else ("🔑 Token only (no DB)" if n_key else "⬜ Not connected")
    usage  = get_usage(team_id)
    pct    = int((usage / FREE_LIMIT) * 100)
    bar    = ("🟧" * (pct // 20)) + ("⬜" * (5 - pct // 20))

    return {
        "type": "home",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "HushAsk Command Center ⚙️", "emoji": True}},
            {"type": "section", "text": {"type": "mrkdwn", "text": "_Anonymous Slack router · by HonestAlias_"}},
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Current Configuration*"}},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*📢 Public Channel*\n{pub}"},
                    {"type": "mrkdwn", "text": f"*🔒 Private Channel*\n{hr}"},
                    {"type": "mrkdwn", "text": f"*📄 Notion Vault*\n{notion}"},
                    {"type": "mrkdwn", "text": f"*Last updated*\n{config['updated_at'] or '—'}"},
                ]
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*📊 Monthly Usage* — {usage} / {FREE_LIMIT} messages\n{bar}  {pct}%"}
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button", "action_id": "edit_settings",
                        "text": {"type": "plain_text", "text": "✏️ Edit Settings", "emoji": True},
                        "style": "primary"
                    },
                    {
                        "type": "button", "action_id": "reset_config",
                        "text": {"type": "plain_text", "text": "🔄 Reset", "emoji": True},
                        "style": "danger",
                        "confirm": {
                            "title": {"type": "plain_text", "text": "Reset configuration?"},
                            "text": {"type": "mrkdwn", "text": "Clears all routing and Notion settings. Message history is preserved."},
                            "confirm": {"type": "plain_text", "text": "Yes, reset"},
                            "deny":    {"type": "plain_text", "text": "Cancel"},
                            "style": "danger"
                        }
                    }
                ]
            },
            {"type": "divider"},
            {"type": "context", "elements": [{"type": "mrkdwn", "text": f"🔒 Admin-only panel · <{HELP_BASE}/|Help Center>"}]}
        ]
    }


# ── Wizard modals ─────────────────────────────────────────────────────────────

def wizard_step1() -> dict:
    """Step 1 — Introduction with 4-step sequence."""
    return {
        "type": "modal",
        "callback_id": "wizard_step1",
        "title": {"type": "plain_text", "text": "HushAsk Setup (1/3)"},
        "submit": {"type": "plain_text", "text": "Get Started →"},
        "close":  {"type": "plain_text", "text": "Cancel"},
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Turning transient chat into a permanent library.", "emoji": True}
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "HushAsk gives your team a safe, anonymous way to speak up — and turns every answered question into lasting company knowledge.\n\n*Here's the full sequence:*"}
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": "*1️⃣  DM the bot*\nAnyone on your team sends a message directly to HushAsk."},
                    {"type": "mrkdwn", "text": "*2️⃣  Choose a route*\n📢 Public Knowledge or 🔒 Private / HR — sender decides."},
                    {"type": "mrkdwn", "text": "*3️⃣  Team response*\nThe right people see it in the right channel and respond."},
                    {"type": "mrkdwn", "text": "*4️⃣  Sync to Notion _(optional)_*\nOne click turns the answered Q&A into a permanent doc."},
                ]
            },
            {"type": "divider"},
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "The Notion step is completely optional — HushAsk works great without it. This wizard takes about 2 minutes."}]
            }
        ]
    }


def wizard_step2_modal(auto_create: bool = True, meta: dict = None) -> dict:
    """Step 2 — Infrastructure. Conditionally shows channel selects."""
    if meta is None:
        meta = {}

    auto_create_block = {
        "type": "input",
        "block_id": "block_auto_create",
        "label": {"type": "plain_text", "text": "🔧 Channel setup"},
        "optional": True,
        "element": {
            "type": "checkboxes",
            "action_id": "auto_create_check",
            "options": [{
                "text": {"type": "mrkdwn", "text": "*Create channels for me*\nSpins up `#hush-public` (📢 Public) and `#hush-hr` (🔒 Private) — both managed by HushAsk."},
                "value": "auto_create"
            }],
            **({"initial_options": [{"text": {"type": "mrkdwn", "text": "*Create channels for me*\nSpins up `#hush-public` (📢 Public) and `#hush-hr` (🔒 Private) — both managed by HushAsk."}, "value": "auto_create"}]} if auto_create else {})
        }
    }

    blocks = [
        {"type": "header", "text": {"type": "plain_text", "text": "Infrastructure — Triage Channels"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "HushAsk routes messages to two channels: one *Public* (open to all) and one *Private* (confidential, HR-level access)."}},
        {"type": "divider"},
        auto_create_block,
    ]

    if auto_create:
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "✅ *We'll create both channels automatically* — you can rename them any time.\n\n_Uncheck the box above to pick existing channels instead._"}
        })
    else:
        pub_el = {"type": "conversations_select", "action_id": "public_channel_select",
                  "placeholder": {"type": "plain_text", "text": "Select a channel"},
                  "filter": {"include": ["public"], "exclude_bot_users": True}}
        hr_el  = {"type": "conversations_select", "action_id": "hr_channel_select",
                  "placeholder": {"type": "plain_text", "text": "Select a channel"},
                  "filter": {"include": ["private"], "exclude_bot_users": True}}

        if meta.get("public_channel"): pub_el["initial_conversation"] = meta["public_channel"]
        if meta.get("hr_channel"):     hr_el["initial_conversation"]  = meta["hr_channel"]

        blocks += [
            {"type": "divider"},
            {"type": "input", "block_id": "block_public_channel",
             "label": {"type": "plain_text", "text": "📢 Public Channel"},
             "hint": {"type": "plain_text", "text": "Anonymous messages routed here are visible to the whole team."},
             "optional": False,
             "element": pub_el},
            {"type": "input", "block_id": "block_hr_channel",
             "label": {"type": "plain_text", "text": "🔒 Private Channel"},
             "hint": {"type": "plain_text", "text": "Sensitive messages route here — keep this private."},
             "optional": False,
             "element": hr_el},
        ]

    return {
        "type": "modal",
        "callback_id": "wizard_step2",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "HushAsk Setup (2/3)"},
        "submit": {"type": "plain_text", "text": "Continue →"},
        "close":  {"type": "plain_text", "text": "Back"},
        "blocks": blocks
    }


def wizard_step3(meta: dict) -> dict:
    """Step 3 — Notion Vault. OAuth button if client_id set, else manual fields."""
    has_oauth = bool(NOTION_CLIENT_ID)
    notion_state = meta.get("notion_state", "")

    if has_oauth:
        oauth_url = (
            "https://api.notion.com/v1/oauth/authorize"
            f"?client_id={NOTION_CLIENT_ID}"
            "&response_type=code&owner=user"
            f"&redirect_uri={urllib.parse.quote(NOTION_REDIRECT)}"
            f"&state={notion_state}"
        )
        vault_blocks = [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "Click below to authorize HushAsk in your Notion workspace. We'll automatically create a *Hush Library* database where answered messages are stored — no page IDs or tokens required."
                }
            },
            {"type": "divider"},
            {
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🔗 Connect to Notion", "emoji": True},
                    "style": "primary",
                    "url": oauth_url,
                    "action_id": "notion_oauth_click"
                }]
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"After connecting in your browser, return here and click *Save & Finish*. · <{HELP_BASE}/setting-up-notion.html|Setup guide>"}]
            }
        ]
    else:
        # Manual fallback (internal integration token)
        vault_blocks = [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"Provide your Notion integration token and the ID of your *Hush Library* database. <{HELP_BASE}/setting-up-notion.html|How to set this up →>"}
            },
            {"type": "divider"},
            {
                "type": "input", "block_id": "block_notion_token",
                "label": {"type": "plain_text", "text": "Notion API Token"},
                "optional": True,
                "hint": {"type": "plain_text", "text": "From notion.so/my-integrations. Starts with secret_"},
                "element": {
                    "type": "plain_text_input", "action_id": "notion_token_input",
                    "placeholder": {"type": "plain_text", "text": "secret_..."},
                    "initial_value": meta.get("notion_api_key", "")
                }
            },
            {
                "type": "input", "block_id": "block_notion_db",
                "label": {"type": "plain_text", "text": "Hush Library Database ID"},
                "optional": True,
                "hint": {"type": "plain_text", "text": "32-character ID from the database URL. Share the database with your integration first."},
                "element": {
                    "type": "plain_text_input", "action_id": "notion_db_input",
                    "placeholder": {"type": "plain_text", "text": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"},
                    "initial_value": meta.get("notion_database_id", "")
                }
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Both fields are optional — you can skip and add Notion later from Settings."}]
            }
        ]

    return {
        "type": "modal",
        "callback_id": "wizard_step3",
        "private_metadata": json.dumps(meta),
        "title": {"type": "plain_text", "text": "HushAsk Setup (3/3)"},
        "submit": {"type": "plain_text", "text": "Save & Finish ✓"},
        "close":  {"type": "plain_text", "text": "Back"},
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": "The Notion Vault — Optional", "emoji": True}},
            *vault_blocks,
        ]
    }


# ── Home publish helper ───────────────────────────────────────────────────────

def publish_home(client, user_id: str, team_id: str):
    config       = get_workspace_config(team_id)
    installer_id = config["installer_id"] if config else None
    if is_admin(client, user_id) or user_id == installer_id:
        view = home_configured(config, client, team_id) if config else home_unconfigured()
    else:
        view = home_welcome()
    client.views_publish(user_id=user_id, view=view)


@app.event("app_home_opened")
def handle_home_opened(event, client):
    publish_home(client, event["user"], resolve_team_id(client))


# ── Wizard actions ────────────────────────────────────────────────────────────

def _open_wizard(ack, body, client):
    ack()
    team_id = body["team"]["id"]
    config  = get_workspace_config(team_id)
    meta = {}
    if config:
        meta = {
            "public_channel":    config["public_channel"] or "",
            "hr_channel":        config["hr_channel"] or "",
            "notion_api_key":    config["notion_api_key"] or "",
            "notion_database_id": config["notion_database_id"] or "",
        }
    client.views_open(trigger_id=body["trigger_id"], view=wizard_step1())

app.action("start_setup")(_open_wizard)
app.action("edit_settings")(_open_wizard)


@app.action("reset_config")
def handle_reset(ack, body, client):
    ack()
    reset_workspace_config(body["team"]["id"])
    publish_home(client, body["user"]["id"], body["team"]["id"])


@app.action("auto_create_check")
def handle_auto_create_toggle(ack, body, client):
    """True conditional UI: toggle channel selects on checkbox change."""
    ack()
    selected    = body["actions"][0].get("selected_options", [])
    auto_create = any(o["value"] == "auto_create" for o in selected)
    meta        = json.loads(body["view"].get("private_metadata", "{}"))
    client.views_update(view_id=body["view"]["id"], view=wizard_step2_modal(auto_create=auto_create, meta=meta))


@app.action("notion_oauth_click")
def handle_notion_oauth_click(ack): ack()


# ── Wizard view submissions ───────────────────────────────────────────────────

@app.view("wizard_step1")
def wizard1_submit(ack):
    ack(response_action="push", view=wizard_step2_modal(auto_create=True))


@app.view("wizard_step2")
def wizard2_submit(ack, body):
    values = body["view"]["state"]["values"]
    meta   = json.loads(body["view"].get("private_metadata", "{}"))
    team_id = body["team"]["id"]

    auto_opts   = values.get("block_auto_create", {}).get("auto_create_check", {}).get("selected_options", [])
    auto_create = any(o["value"] == "auto_create" for o in auto_opts)

    pub_ch = ""
    hr_ch  = ""
    if not auto_create:
        pub_ch = values.get("block_public_channel", {}).get("public_channel_select", {}).get("selected_conversation") or ""
        hr_ch  = values.get("block_hr_channel",     {}).get("hr_channel_select",    {}).get("selected_conversation") or ""
        if not pub_ch or not hr_ch:
            ack(response_action="errors", errors={
                "block_public_channel": "Select a public channel or enable auto-create above.",
                "block_hr_channel":     "Select a private channel or enable auto-create above.",
            })
            return

    # Generate Notion OAuth state token for step 3
    notion_state = secrets.token_hex(16)
    store_notion_state(notion_state, team_id)

    meta.update({
        "team_id":       team_id,
        "auto_create":   auto_create,
        "public_channel": pub_ch,
        "hr_channel":    hr_ch,
        "notion_state":  notion_state,
    })
    ack(response_action="push", view=wizard_step3(meta))


@app.view("wizard_step3")
def wizard3_submit(ack, body, client):
    ack()
    team_id = body["team"]["id"]
    user_id = body["user"]["id"]
    meta    = json.loads(body["view"].get("private_metadata", "{}"))
    values  = body["view"]["state"]["values"]

    # Channel setup
    pub_ch = meta.get("public_channel", "")
    hr_ch  = meta.get("hr_channel", "")
    if meta.get("auto_create"):
        pub_id = find_or_create_channel(client, "hush-public", is_private=False)
        hr_id  = find_or_create_channel(client, "hush-hr",     is_private=True)
        if pub_id: pub_ch = pub_id
        if hr_id:  hr_ch  = hr_id

    # Notion: check OAuth result first, then manual fields, then existing config
    existing    = get_workspace_config(team_id)
    notion_key  = existing["notion_api_key"]     if existing else None
    notion_db   = existing["notion_database_id"] if existing else None

    if not NOTION_CLIENT_ID:
        # Manual fields path
        manual_key = (values.get("block_notion_token", {}).get("notion_token_input", {}).get("value") or "").strip() or None
        manual_db  = (values.get("block_notion_db",    {}).get("notion_db_input",    {}).get("value") or "").strip() or None
        if manual_key: notion_key = manual_key
        if manual_db:  notion_db  = manual_db

    installer_id = existing["installer_id"] if (existing and existing["installer_id"]) else user_id
    save_workspace_config(team_id, installer_id, pub_ch, hr_ch, notion_key, notion_db)
    publish_home(client, user_id, team_id)


# ── Example prompts (non-admin home) ─────────────────────────────────────────

@app.action(re.compile(r"^example_(tech|feedback|idea)$"))
def handle_example(ack, body, client):
    """Open a DM with the user pre-populated with routing blocks for the example."""
    ack()
    action_id = body["actions"][0]["action_id"]
    user_id   = body["user"]["id"]
    team_id   = body["team"]["id"]
    text      = EXAMPLE_MESSAGES.get(action_id, "")
    if not text: return

    dm_result = client.conversations_open(users=user_id)
    dm_ch     = dm_result["channel"]["id"]

    user_hash = hash_user(user_id, team_id)
    token     = make_token(user_id, team_id)
    result    = client.chat_postMessage(channel=dm_ch, blocks=routing_blocks(token, text), text="Route your message:")
    save_pending(token, team_id, dm_ch, text, user_hash, result.get("ts"))


# ── Block builders ────────────────────────────────────────────────────────────

def routing_blocks(token, message):
    preview = message if len(message) <= 280 else message[:277] + "…"
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "🔒 *Your identity has been anonymized.* Choose how to route your message:"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Your message:*\n>{preview}"}},
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {"type": "button", "action_id": "route_public", "style": "primary",
                 "text": {"type": "plain_text", "text": "📢 Public", "emoji": True}, "value": token},
                {"type": "button", "action_id": "route_hr", "style": "danger",
                 "text": {"type": "plain_text", "text": "🔒 Private / HR", "emoji": True}, "value": token},
            ]
        },
        {"type": "context", "elements": [{"type": "mrkdwn", "text": "Your Slack identity will never be stored or shared."}]}
    ]


def confirmed_blocks(label):
    return [{"type": "section", "text": {"type": "mrkdwn", "text": f"✅ *Delivered anonymously.*\nRouted to: *{label}*"}}]


def triage_blocks(message, label, msg_id, has_notion):
    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": f"{label}\n\n{message}"}}]
    if has_notion:
        blocks.append({"type": "actions", "elements": [{
            "type": "button", "action_id": "sync_notion",
            "text": {"type": "plain_text", "text": "📄 Sync to Notion", "emoji": True},
            "value": str(msg_id)
        }]})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": "🔒 Delivered anonymously via HushAsk"}]})
    return blocks


def limit_blocks(usage):
    return [
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"⚠️ *You've hit the free tier limit.*\nYour workspace has sent *{usage}/{FREE_LIMIT}* anonymous messages this month.\n\nUpgrade to Pro for unlimited routing, priority support, and advanced analytics."}
        },
        {
            "type": "actions",
            "elements": [{
                "type": "button", "action_id": "upgrade_click",
                "style": "primary",
                "text": {"type": "plain_text", "text": "🚀 Upgrade to Pro", "emoji": True},
                "url": UPGRADE_URL
            }]
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": "Resets automatically at the start of your next billing month."}]
        }
    ]


# ── Incoming messages ─────────────────────────────────────────────────────────

def handle_incoming(client, team_id, user_id, channel_id, text):
    if not text or not text.strip():
        client.chat_postMessage(channel=channel_id, text="Send me a message and I'll help you route it anonymously. 🔒")
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
    handle_incoming(client, message["team"], message["user"], message["channel"], message.get("text", ""))

@app.event("app_mention")
def on_mention(event, client):
    handle_incoming(client, event["team"], event["user"], event["channel"], event.get("text", ""))


# ── Route actions ─────────────────────────────────────────────────────────────

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

    # Freemium gate
    allowed, usage = check_and_increment(team_id)
    if not allowed:
        try:
            client.chat_update(channel=src, ts=msg_ts, blocks=limit_blocks(usage), text="Monthly limit reached.")
        except Exception:
            client.chat_postEphemeral(channel=src, user=user_id, blocks=limit_blocks(usage), text="Monthly limit reached.")
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

@app.action("upgrade_click")
def handle_upgrade(ack): ack()


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
        client.chat_postEphemeral(channel=channel, user=user_id,
            text=f"⚠️ Notion isn't configured. Open App Home → Settings. <{HELP_BASE}/setting-up-notion.html|Setup guide>")
        return

    ok, err = push_to_notion(config["notion_api_key"], config["notion_database_id"],
                              delivered["message"], delivered["route_type"])
    if ok:
        mark_notion_synced(msg_id)
        try:
            new_blocks = [
                {"type": "section", "text": {"type": "mrkdwn", "text": body["message"]["blocks"][0]["text"]["text"]}},
                {"type": "context", "elements": [{"type": "mrkdwn", "text": "✅ Synced to Notion · 🔒 Delivered anonymously via HushAsk"}]}
            ]
            client.chat_update(channel=channel, ts=msg_ts, blocks=new_blocks, text="Synced.")
        except Exception: pass
    else:
        client.chat_postEphemeral(channel=channel, user=user_id, text=f"⚠️ Notion sync failed: {err}")


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    print("[HushAsk] Starting in Socket Mode...")
    SocketModeHandler(app, os.environ["SLACK_APP_TOKEN"]).start()
