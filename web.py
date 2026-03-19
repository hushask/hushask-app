"""
web.py — HushAsk unified Flask server
Routes:
  /slack/install          → Slack OAuth install flow
  /slack/oauth_redirect   → Slack OAuth callback
  /slack/events           → Slack Events API
  /slack/interactive      → Slack Interactivity (actions, views)
  /notion/callback        → Notion OAuth callback
  /notion/connected       → Notion OAuth success page
  /notion/error           → Notion OAuth error page
  /upgrade                → Generate Stripe Checkout Session
  /upgrade/success        → Post-payment landing + DM "Welcome to Pro"
  /stripe/webhook         → Stripe event listener
  /health                 → Healthcheck
  /* (static)             → Landing page, help, assets
"""

import os
import requests as http
from flask import Flask, request, redirect, send_from_directory, jsonify, render_template_string
from dotenv import load_dotenv
load_dotenv()

import stripe
from slack_bolt.adapter.flask import SlackRequestHandler
import app as bolt_module

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
PORT      = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))
API_BASE  = os.environ.get("API_BASE", "https://hushask.com")
SITE_BASE = os.environ.get("SITE_BASE", "https://hushask.com")

NOTION_CLIENT_ID     = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.environ.get("NOTION_CLIENT_SECRET", "")
NOTION_REDIRECT      = os.environ.get("NOTION_REDIRECT_URI", f"{API_BASE}/notion/callback")

STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID   = os.environ.get("STRIPE_PRO_PRICE_ID", "")
stripe.api_key        = os.environ.get("STRIPE_SECRET_KEY", "")

BLOCKED = {".env", ".env.save", ".env.example", "hushask.db", "app.py",
           "database.py", "web.py", "requirements.txt", "start.sh",
           "start-railway.sh", "railway.toml", "Dockerfile"}

handler = SlackRequestHandler(bolt_module.app)
web = Flask(__name__, static_folder=None)


# ── Health ─────────────────────────────────────────────────────────────────────

@web.route("/health")
def health():
    return jsonify({"status": "ok", "service": "hushask"}), 200


# ── Slack routes ───────────────────────────────────────────────────────────────

@web.route("/slack/install")
def slack_install():
    return handler.handle(request)

@web.route("/slack/oauth_redirect")
def slack_oauth_redirect():
    return handler.handle(request)

@web.route("/slack/events", methods=["POST"])
def slack_events():
    return handler.handle(request)

@web.route("/slack/interactive", methods=["POST"])
def slack_interactive():
    # Drop Slack's automatic retries for interactive payloads (view submissions,
    # block actions). Our view handlers are idempotent and retries cause the
    # wizard to restart after it already completed successfully.
    if request.headers.get("X-Slack-Retry-Num"):
        print(f"[slack/interactive] dropping retry #{request.headers.get('X-Slack-Retry-Num')}")
        return "", 200
    return handler.handle(request)

@web.route("/slack/options", methods=["POST"])
def slack_options():
    return handler.handle(request)


# ── Notion OAuth ───────────────────────────────────────────────────────────────

@web.route("/notion/connect")
def notion_connect():
    """OAuth kickoff — browser GET, no Slack headers required."""
    team_id = request.args.get("team_id", "")
    if not team_id or not NOTION_CLIENT_ID:
        return "Missing configuration", 400
    import secrets
    from database import store_notion_state
    state = secrets.token_urlsafe(32)
    store_notion_state(state, team_id)
    notion_auth_url = (
        f"https://api.notion.com/v1/oauth/authorize"
        f"?client_id={NOTION_CLIENT_ID}"
        f"&response_type=code"
        f"&owner=user"
        f"&redirect_uri={NOTION_REDIRECT}"
        f"&state={state}"
    )
    return redirect(notion_auth_url)

@web.route("/notion/callback")
def notion_callback():
    try:
        code  = request.args.get("code")
        state = request.args.get("state")
        error = request.args.get("error")

        if error or not code:
            return redirect(f"/notion/error?reason={error or 'missing_code'}")

        try:
            r = http.post(
                "https://api.notion.com/v1/oauth/token",
                auth=(NOTION_CLIENT_ID, NOTION_CLIENT_SECRET),
                json={"grant_type": "authorization_code", "code": code, "redirect_uri": NOTION_REDIRECT},
                headers={"Content-Type": "application/json"},
                timeout=15
            )
            data = r.json()
            if r.status_code != 200 or "access_token" not in data:
                msg = data.get("error_description") or data.get("error") or f"HTTP {r.status_code}"
                return redirect(f"/notion/error?reason={msg}")
        except Exception:
            return redirect("/notion/error?reason=exchange_failed")

        access_token = data["access_token"]
        from database import get_team_from_state, delete_notion_state, save_workspace_notion, get_workspace_config
        team_id = get_team_from_state(state) if state else None

        # Fix 4a — Duplicate prevention: if workspace already has a Notion DB, just refresh the token
        if team_id:
            existing_config = get_workspace_config(team_id)
            if existing_config and existing_config.get("notion_database_id"):
                save_workspace_notion(team_id, access_token, existing_config["notion_database_id"])
                if state: delete_notion_state(state)
                return redirect("/notion/connected")

        # Fix 4b — No pages authorized guard: Notion grants a valid token even if no pages selected
        owner_type     = data.get("owner", {}).get("type", "")
        workspace_type = data.get("workspace_type", "")
        if owner_type != "workspace" and not data.get("duplicated_template_id"):
            test_r = http.post(
                "https://api.notion.com/v1/search",
                json={"page_size": 1},
                headers={"Authorization": f"Bearer {access_token}", "Notion-Version": "2022-06-28"},
                timeout=10,
            )
            test_data = test_r.json() if test_r.status_code == 200 else {}
            if not test_data.get("results"):
                return redirect("/notion/error?reason=no_pages_authorized")

        # Fix 4c — Guard against None db_id from _provision_hush_library
        db_id, db_url = _provision_hush_library(access_token)
        if not db_id:
            return redirect("/notion/error?reason=database_creation_failed")
        if team_id:
            save_workspace_notion(team_id, access_token, db_id)
            if state: delete_notion_state(state)
            # Refresh App Home so the Notion button flips to "Disconnect"
            try:
                from database import get_conn
                import sqlite3
                with get_conn() as _conn:
                    _conn.row_factory = sqlite3.Row
                    ws = _conn.execute(
                        "SELECT bot_token, installer_user_id FROM workspaces WHERE team_id = ?",
                        (team_id,)
                    ).fetchone()
                if ws and ws["bot_token"] and ws["installer_user_id"]:
                    from slack_sdk import WebClient as _WebClient
                    from app import publish_home as _publish_home
                    _client = _WebClient(token=ws["bot_token"])
                    _publish_home(_client, ws["installer_user_id"], team_id)
            except Exception as _e:
                print(f"[notion_callback] App Home refresh failed (non-fatal): {_e}")

        return redirect("/notion/connected" + (f"?db_url={db_url}" if db_url else ""))

    except Exception as e:
        import traceback
        import logging as _logging
        _logging.getLogger(__name__).error(f"[notion_callback] Unhandled exception: {traceback.format_exc()}")
        return render_template_string("""<!DOCTYPE html>
<html>
<head><title>HushAsk — Notion Connection Error</title></head>
<body style="font-family: sans-serif; max-width: 480px; margin: 80px auto; color: #2C3E50;">
  <h2>Unable to Connect Notion</h2>
  <p>An error occurred during authorization. Please return to Slack and try again.</p>
  <p style="color: #95A5A6; font-size: 13px;">Error: {{ error }}</p>
</body>
</html>""", error=str(e)), 200


def _provision_hush_library(token):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Notion-Version": "2022-06-28"}

    # ── Check for existing Hush Library before creating a new one ───────────
    # This prevents duplicate DBs on every Reset → re-wizard cycle.
    try:
        s = http.post(
            "https://api.notion.com/v1/search",
            json={"query": "Hush Library", "filter": {"value": "database", "property": "object"}, "page_size": 10},
            headers=headers, timeout=10
        )
        if s.status_code == 200:
            for db in s.json().get("results", []):
                title_parts = db.get("title", [])
                title_text  = title_parts[0].get("text", {}).get("content", "") if title_parts else ""
                if title_text == "Hush Library":
                    db_id  = db["id"]
                    db_url = db.get("url") or f"https://notion.so/{db_id.replace('-', '')}"
                    print(f"[notion] ✅ Reusing existing Hush Library: {db_id}")
                    return db_id, db_url
    except Exception as e:
        print(f"[notion] search-before-create failed: {e}")

    db_props = {
        "title": [{"type": "text", "text": {"content": "Hush Library"}}],
        "icon": {"type": "emoji", "emoji": "🔒"},
        "properties": {
            "Name": {"title": {}},
            "Route": {"select": {"options": [{"name": "📢 Public", "color": "blue"}, {"name": "🔒 Confidential / HR", "color": "red"}]}},
            "Status": {"select": {"options": [{"name": "New", "color": "yellow"}, {"name": "Answered", "color": "green"}, {"name": "Archived", "color": "gray"}]}},
            "Synced At": {"date": {}},
        }
    }

    # Strategy 1: workspace root (works if user granted full workspace access)
    # Strategy 2: fall back to the first page the integration has access to
    parents_to_try = [{"type": "workspace", "workspace": True}]

    # Discover pages the token has access to as fallback parents
    try:
        search_r = http.post(
            "https://api.notion.com/v1/search",
            json={"filter": {"value": "page", "property": "object"}, "page_size": 5},
            headers=headers, timeout=10
        )
        if search_r.status_code == 200:
            pages = search_r.json().get("results", [])
            print(f"[notion] accessible pages: {[p.get('id') for p in pages[:5]]}")
            for page in pages:
                parents_to_try.append({"type": "page_id", "page_id": page["id"]})
    except Exception as e:
        print(f"[notion] search error: {e}")

    for parent in parents_to_try:
        try:
            payload = {"parent": parent, **db_props}
            r = http.post("https://api.notion.com/v1/databases", json=payload, headers=headers, timeout=15)
            print(f"[notion] create attempt parent={parent} → HTTP {r.status_code}")
            print(f"[notion] response body: {r.text[:500]}")
            if r.status_code == 200:
                db     = r.json()
                db_id  = db["id"]
                db_url = db.get("url") or f"https://notion.so/{db_id.replace('-','')}"
                parent_info = db.get("parent", {})
                print(f"[notion] ✅ Hush Library created! id={db_id} url={db_url} parent={parent_info}")
                return db_id, db_url
        except Exception as e:
            print(f"[notion] create exception with parent={parent}: {e}")

    print("[notion] ❌ All parent strategies failed — could not create Hush Library")
    return None, None


# ── Stripe Checkout ────────────────────────────────────────────────────────────

@web.route("/upgrade")
def upgrade():
    team_id = request.args.get("team_id", "")

    if not stripe.api_key or not STRIPE_PRO_PRICE_ID:
        return redirect(f"{SITE_BASE}/#early-access")

    try:
        session = stripe.checkout.Session.create(
            payment_method_types=["card"],
            line_items=[{"price": STRIPE_PRO_PRICE_ID, "quantity": 1}],
            mode="subscription",
            allow_promotion_codes=True,
            success_url=f"{API_BASE}/upgrade/success?team_id={team_id}&session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=SITE_BASE,
            metadata={"team_id": team_id},
            subscription_data={"metadata": {"team_id": team_id}},
        )
        return redirect(session.url, 303)
    except Exception as e:
        print(f"[stripe/checkout] error: {e}")
        return redirect(f"{SITE_BASE}/#early-access")


@web.route("/upgrade/success")
def upgrade_success():
    team_id    = request.args.get("team_id", "")
    session_id = request.args.get("session_id", "")
    # DM the installer with the Pro welcome message
    if team_id:
        _send_pro_welcome(team_id)
    return _render_pro_success_page()


@web.route("/stripe/webhook", methods=["POST"])
def stripe_webhook():
    payload = request.data
    sig     = request.headers.get("Stripe-Signature", "")

    if not STRIPE_WEBHOOK_SECRET:
        return "", 400

    try:
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)
    except ValueError:
        return "Invalid payload", 400
    except stripe.error.SignatureVerificationError:
        return "Invalid signature", 400

    if event["type"] == "checkout.session.completed":
        sess    = event["data"]["object"]
        team_id = (sess.get("metadata") or {}).get("team_id")
        if team_id:
            from database import upgrade_to_pro
            upgrade_to_pro(team_id)
            _send_pro_welcome(team_id)
            print(f"[stripe] Workspace {team_id} upgraded to Pro.")

    elif event["type"] == "customer.subscription.deleted":
        sub     = event["data"]["object"]
        team_id = (sub.get("metadata") or {}).get("team_id")
        if team_id:
            from database import revoke_pro
            revoke_pro(team_id)
            _send_downgrade_notice(team_id)
            print(f"[stripe] Workspace {team_id} downgraded to free tier.")

    return "", 200


def _send_pro_welcome(team_id: str):
    from database import find_bot_token, get_workspace_config
    from slack_sdk import WebClient
    bot_token = find_bot_token(None, team_id)
    config    = get_workspace_config(team_id)
    if not bot_token or not config: return
    installer_id = config.get("installer_id") if config else None
    if not installer_id: return
    try:
        client = WebClient(token=bot_token)
        dm     = client.conversations_open(users=installer_id)["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            blocks=bolt_module.pro_welcome_blocks(),
            text="HushAsk Pro is active."
        )
    except Exception as e:
        print(f"[pro_welcome] error: {e}")


def _send_downgrade_notice(team_id: str):
    """DM the installer when their Pro subscription is cancelled."""
    from database import find_bot_token, get_workspace_config
    from slack_sdk import WebClient
    bot_token = find_bot_token(None, team_id)
    config    = get_workspace_config(team_id)
    if not bot_token or not config: return
    installer_id = config.get("installer_id") if config else None
    if not installer_id: return
    try:
        client = WebClient(token=bot_token)
        dm     = client.conversations_open(users=installer_id)["channel"]["id"]
        client.chat_postMessage(
            channel=dm,
            text="HushAsk Pro subscription cancelled.",
            blocks=[
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "*Pro subscription ended.*\n\n"
                    f"Workspace reverted to free tier (*{os.environ.get('FREE_LIMIT', '20')} msgs/month*). "
                    "Config and Notion sync preserved. Reactivate any time."
                }},
                {"type": "actions", "elements": [
                    {"type": "button",
                     "text": {"type": "plain_text", "text": "Reactivate Pro"},
                     "style": "primary",
                     "url": f"{API_BASE}/upgrade?team_id={team_id}",
                     "action_id": "reactivate_pro_cta"}
                ]}
            ]
        )
        print(f"[downgrade_notice] Sent to {installer_id} for workspace {team_id}")
    except Exception as e:
        print(f"[downgrade_notice] error: {e}")


# ── Static serving ─────────────────────────────────────────────────────────────

@web.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

@web.route("/privacy")
def privacy():
    return send_from_directory(BASE_DIR, "privacy.html")

@web.route("/terms")
def terms():
    return send_from_directory(BASE_DIR, "terms.html")

@web.route("/pricing")
def pricing():
    return send_from_directory(BASE_DIR, "pricing.html")

@web.route("/help/")
def help_index():
    return send_from_directory(os.path.join(BASE_DIR, "help"), "index.html")

@web.route("/help/<path:filename>")
def help_file(filename):
    return send_from_directory(os.path.join(BASE_DIR, "help"), filename)

@web.route("/assets/<path:filename>")
def assets_file(filename):
    return send_from_directory(os.path.join(BASE_DIR, "assets"), filename)

@web.route("/<path:filename>")
def root_file(filename):
    if filename in BLOCKED or filename.startswith("."):
        return "403 Forbidden", 403
    ext = os.path.splitext(filename)[1].lower()
    if ext not in {".html", ".css", ".js", ".svg", ".png", ".ico", ".txt", ".webmanifest"}:
        return "403 Forbidden", 403
    return send_from_directory(BASE_DIR, filename)


# ── Page templates ─────────────────────────────────────────────────────────────

_PAGE_STYLE = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#F8FAFC;color:#0F172A;font-family:'Inter',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:white;border:1px solid #E2E8F0;border-radius:20px;padding:48px 52px;text-align:center;max-width:480px;width:90%}
  .icon{font-size:52px;margin-bottom:24px}
  h1{font-size:26px;font-weight:800;margin-bottom:12px}
  p{color:#475569;font-size:15px;line-height:1.6;margin-bottom:20px}
  .badge{display:inline-block;padding:4px 14px;border-radius:100px;font-size:12px;font-weight:700}
  .ok{background:#F0FDF4;color:#16A34A;border:1px solid #BBF7D0}
  .err{background:#FEF2F2;color:#DC2626;border:1px solid #FECACA}
  .pro{background:#1A2E62;color:white;border:none;padding:6px 18px;font-size:13px}
  a{color:#2563EB;text-decoration:none}
  code{font-family:monospace;background:#F1F5F9;border:1px solid #E2E8F0;padding:2px 6px;border-radius:4px;font-size:13px;color:#0F172A}
</style>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;800&display=swap" rel="stylesheet">
"""

def _render_pro_success_page():
    return f"""<!DOCTYPE html><html><head><title>Pro Active — HushAsk</title>{_PAGE_STYLE}</head>
<body><div class="card">
  <div class="icon">🎉</div>
  <h1>Pro Plan Active</h1>
  <p>Unlimited routing is now active. A confirmation was sent to the installer via Slack DM.</p>
  <span class="badge pro">Pro — Active</span>
</div></body></html>"""

@web.route("/notion/connected")
def notion_connected():
    db_url = request.args.get("db_url", "")
    link   = f'<br><br><a href="{db_url}" target="_blank">Open Hush Library in Notion →</a>' if db_url else ""
    return f"""<!DOCTYPE html><html><head><title>Connected — HushAsk</title>{_PAGE_STYLE}</head>
<body><div class="card">
  <div class="icon">✅</div>
  <h1>Notion Connected</h1>
  <p><strong>Hush Library</strong> is ready. Your Slack App Home has been updated — you can close this tab.{link}</p>
  <span class="badge ok">Connected</span>
</div></body></html>"""

@web.route("/notion/error")
def notion_error():
    reason = request.args.get("reason", "Unknown error")
    return f"""<!DOCTYPE html><html><head><title>Error — HushAsk</title>{_PAGE_STYLE}</head>
<body><div class="card">
  <div class="icon">⚠️</div>
  <h1>Connection Failed</h1>
  <p>Notion connection failed.<br><code>{reason}</code></p>
  <p>Return to Slack and try again, or check the <a href="/help/setting-up-notion.html">setup guide</a>.</p>
  <span class="badge err">Failed</span>
</div></body></html>"""


# ── Entrypoint ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[web] Starting on port {PORT}...")
    web.run(host="0.0.0.0", port=PORT, debug=False)
