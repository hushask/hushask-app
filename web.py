"""
web.py — HushAsk static + OAuth web server (Flask on port 8080)
Replaces: python3 -m http.server 8080
Handles:
  - Controlled static file serving (blocks .env, *.py, *.db)
  - GET /notion/callback  — Notion OAuth exchange + Hush Library provisioning
  - GET /notion/connected — Success confirmation page
  - GET /notion/error     — Error page
"""

import os, sys
import requests as http
from flask import Flask, request, redirect, send_from_directory, render_template_string
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORT     = int(os.environ.get("PORT", os.environ.get("WEB_PORT", "8080")))

NOTION_CLIENT_ID     = os.environ.get("NOTION_CLIENT_ID", "")
NOTION_CLIENT_SECRET = os.environ.get("NOTION_CLIENT_SECRET", "")
NOTION_REDIRECT      = os.environ.get("NOTION_REDIRECT_URI", "http://178.128.28.93:8080/notion/callback")

# Files / dirs explicitly blocked from public serving
BLOCKED = {".env", ".env.save", ".env.example", "hushask.db", "app.py",
           "database.py", "web.py", "requirements.txt", "start.sh", "BOOTSTRAP.md"}
ALLOWED_DIRS = {"help", "assets"}

web = Flask(__name__, static_folder=None)

# ── Health check ──────────────────────────────────────────────────────────────

@web.route("/health")
def health():
    return {"status": "ok", "service": "hushask-web"}, 200


# ── Static serving ────────────────────────────────────────────────────────────

@web.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

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


# ── Notion OAuth callback ─────────────────────────────────────────────────────

_PAGE_STYLE = """
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:#0d0d0d;color:#f0f0f0;font-family:'Inter',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh}
  .card{background:#141414;border:1px solid #222;border-radius:20px;padding:48px 52px;text-align:center;max-width:480px;width:90%}
  .icon{font-size:52px;margin-bottom:24px}
  h1{font-size:26px;font-weight:800;margin-bottom:12px}
  p{color:#888;font-size:15px;line-height:1.6;margin-bottom:24px}
  .badge{display:inline-block;padding:4px 14px;border-radius:100px;font-size:12px;font-weight:700}
  .ok{background:#1a3a1a;color:#28c840;border:1px solid #28c840}
  .err{background:#3a1a1a;color:#ff5f57;border:1px solid #ff5f57}
  a{color:#FF8C42;text-decoration:none}
</style>
"""

@web.route("/notion/callback")
def notion_callback():
    code  = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    if error or not code:
        return redirect("/notion/error?reason=" + (error or "missing_code"))

    # Exchange code for token
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
    except Exception as e:
        return redirect(f"/notion/error?reason=exchange_failed")

    access_token = data["access_token"]

    # Resolve team_id from state
    from database import get_team_from_state, delete_notion_state, save_workspace_notion
    team_id = get_team_from_state(state) if state else None

    # Provision Hush Library database
    db_id, db_url = _provision_hush_library(access_token)

    if team_id and db_id:
        save_workspace_notion(team_id, access_token, db_id)
        delete_notion_state(state)
    elif team_id:
        # Token only — provisioning failed but save what we have
        save_workspace_notion(team_id, access_token, None)
        if state: delete_notion_state(state)

    return redirect("/notion/connected" + (f"?db_url={db_url}" if db_url else ""))


def _provision_hush_library(token: str) -> tuple[str | None, str | None]:
    """Create the Hush Library database in Notion. Returns (database_id, database_url)."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28",
    }
    payload = {
        "parent": {"type": "workspace", "workspace": True},
        "title": [{"type": "text", "text": {"content": "Hush Library"}}],
        "icon": {"type": "emoji", "emoji": "🔒"},
        "properties": {
            "Name": {"title": {}},
            "Route": {"select": {"options": [
                {"name": "📢 Public",          "color": "blue"},
                {"name": "🔒 Confidential / HR", "color": "red"},
            ]}},
            "Status": {"select": {"options": [
                {"name": "New",      "color": "yellow"},
                {"name": "Answered", "color": "green"},
                {"name": "Archived", "color": "gray"},
            ]}},
            "Synced At": {"date": {}},
        }
    }
    try:
        r = http.post("https://api.notion.com/v1/databases", json=payload, headers=headers, timeout=15)
        if r.status_code == 200:
            db = r.json()
            db_id  = db["id"]
            db_url = db.get("url") or f"https://notion.so/{db_id.replace('-','')}"
            return db_id, db_url
        return None, None
    except Exception:
        return None, None


@web.route("/notion/connected")
def notion_connected():
    db_url = request.args.get("db_url", "")
    link   = f'<br><br><a href="{db_url}" target="_blank">Open Hush Library in Notion →</a>' if db_url else ""
    html = f"""<!DOCTYPE html><html><head><title>Connected — HushAsk</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;800&display=swap" rel="stylesheet">
    {_PAGE_STYLE}</head><body>
    <div class="card">
      <div class="icon">✅</div>
      <h1>Notion Connected</h1>
      <p>Your <strong>Hush Library</strong> database has been created. Return to Slack and click <em>Save &amp; Finish</em> to complete setup.{link}</p>
      <span class="badge ok">Connection successful</span>
    </div></body></html>"""
    return html


@web.route("/notion/error")
def notion_error():
    reason = request.args.get("reason", "Unknown error")
    html = f"""<!DOCTYPE html><html><head><title>Error — HushAsk</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;700;800&display=swap" rel="stylesheet">
    {_PAGE_STYLE}</head><body>
    <div class="card">
      <div class="icon">⚠️</div>
      <h1>Connection Failed</h1>
      <p>Something went wrong connecting HushAsk to Notion.<br><code style="color:#FF8C42;font-size:13px">{reason}</code></p>
      <p>Return to Slack and try again, or check the <a href="/help/setting-up-notion.html">setup guide</a>.</p>
      <span class="badge err">Connection failed</span>
    </div></body></html>"""
    return html


# ── Entrypoint ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"[web] Starting on port {PORT}...")
    web.run(host="0.0.0.0", port=PORT, debug=False)
