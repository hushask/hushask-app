# HushAsk

Anonymous message routing for Slack. Employees send questions anonymously and choose where they go — HR for sensitive issues, or a public channel where the team can weigh in. Sender identity is SHA-256 hashed at submission and never stored. Admins never know who asked.

Live at [hushask.com](https://hushask.com) · [Add to Slack](https://hushask.com/slack/install)

---

## Stack

- **Backend:** Python 3.11, Flask, Slack Bolt (HTTP mode)
- **Database:** SQLite on Railway persistent volume
- **Hosting:** Railway (`secure-love` project, `hushask-app` service)
- **Domain:** hushask.com (Cloudflare DNS → Railway)
- **Payments:** Stripe (subscription, webhooks)
- **Notion:** OAuth integration for public thread archiving

---

## Required Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `HASH_SALT` | ✅ | Cryptographically random salt for SHA-256 user ID hashing. Generate: `python3 -c "import secrets; print(secrets.token_hex(32))"` |
| `SLACK_SIGNING_SECRET` | ✅ | From Slack app dashboard → Basic Information |
| `SLACK_CLIENT_ID` | ✅ | From Slack app dashboard → Basic Information |
| `SLACK_CLIENT_SECRET` | ✅ | From Slack app dashboard → Basic Information |
| `DB_PATH` | ✅ | Path to SQLite database. Railway: `/data/hushask.db` (persistent volume mounted at `/data`) |
| `API_BASE` | ✅ | Base URL for the app. Production: `https://hushask.com` |
| `BASE_URL` | ✅ | Same as `API_BASE`. Production: `https://hushask.com` |
| `NOTION_ENCRYPTION_KEY` | ⚠️ | Fernet key for encrypting Notion tokens at rest. Generate: `python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` |
| `NOTION_CLIENT_ID` | Optional | Notion OAuth app client ID |
| `NOTION_CLIENT_SECRET` | Optional | Notion OAuth app client secret |
| `NOTION_REDIRECT_URI` | Optional | Notion OAuth redirect. Default: `{API_BASE}/notion/callback` |
| `STRIPE_SECRET_KEY` | Optional | Stripe secret key (TEST or LIVE) |
| `STRIPE_PRO_PRICE_ID` | Optional | Stripe Price ID for Pro plan |
| `STRIPE_WEBHOOK_SECRET` | Optional | Stripe webhook signing secret |
| `SLACK_APP_ID` | Optional | Slack App ID (shown in App Home build info) |
| `BUILD_ID` | Optional | Git short SHA, injected at deploy time for UI verification |
| `FREE_LIMIT` | Optional | Monthly free message cap. Default: `20` |
| `WEB_WORKERS` | Optional | Gunicorn worker count. Default: `2` |

> **Note:** The app will refuse to start if `HASH_SALT` is missing or set to the default value. `STRIPE_*` and `NOTION_*` vars are optional — features degrade gracefully if missing.

---

## Local Development

### Prerequisites
- Python 3.11+
- A Slack app with the required scopes (see below)
- ngrok or similar for local webhook tunneling

### Setup

```bash
git clone git@github.com:hushask/hushask-app.git
cd hushask-app
pip install -r requirements.txt
```

Create a `.env` file:
```bash
HASH_SALT=your-random-secret-here
SLACK_SIGNING_SECRET=...
SLACK_CLIENT_ID=...
SLACK_CLIENT_SECRET=...
DB_PATH=./hushask.db
API_BASE=https://your-ngrok-url.ngrok.io
BASE_URL=https://your-ngrok-url.ngrok.io
```

Run:
```bash
python web.py
```

### Required Slack Scopes
`chat:write`, `chat:write.public`, `channels:read`, `channels:history`, `channels:manage`, `groups:read`, `groups:write`, `groups:history`, `im:history`, `im:read`, `im:write`, `app_mentions:read`, `users:read`

---

## Railway Deployment

Railway auto-deploys from `main` on GitHub push.

```bash
git push origin main   # triggers Railway build + deploy
```

> ⚠️ `railway redeploy` only redeploys the last built image — it does NOT pick up new commits. Always use `git push origin main`.

The persistent volume must be mounted at `/data` in the Railway dashboard (Volumes section) for the database to survive redeploys.

---

## Running Tests

```bash
pip install pytest pytest-cov
pytest tests/ -v
pytest tests/ --cov=database --cov=app --cov=crypto --cov-report=term-missing
```

44 tests, ~73% coverage. Tests use isolated SQLite files — they never touch the production database.

---

## Key Files

| File | Description |
|------|-------------|
| `app.py` | Slack Bolt handlers — events, actions, views, slash commands |
| `database.py` | SQLite layer — all DB reads/writes |
| `web.py` | Flask server — OAuth flows, Stripe, Notion, static serving |
| `crypto.py` | Fernet encryption for Notion tokens |
| `start-railway.sh` | Railway entrypoint (gunicorn) |
| `Dockerfile` | Docker build config |
| `tests/` | pytest test suite |

---

## Anonymity Model

1. Employee sends a message → Slack user ID + team ID → SHA-256 with `HASH_SALT` → 64-char hex digest stored
2. Original Slack user ID is **never written to disk**
3. `source_channel` (DM channel used for reply delivery) is purged from both `routing_table` and `delivered_messages` after the first admin reply
4. A startup safety sweep NULLs any `source_channel` that slipped through
5. No message content is written to application logs (`RedactUserIdFilter` scrubs user IDs from all log output)

See [hushask.com/help/privacy-and-hashing.html](https://hushask.com/help/privacy-and-hashing.html) for the user-facing explanation.

---

## Architecture Notes

- **Multi-tenant:** One deployment serves all workspaces. Bot tokens stored per-workspace in SQLite.
- **SQLite + WAL:** Suitable for current scale. `busy_timeout=5000` handles concurrent writes. Single persistent volume on Railway.
- **Gunicorn:** 2 workers by default. Each worker maintains its own SQLite connection via `check_same_thread=False`.
- **No message queue:** Slack event handlers ack immediately and defer work to background threads where needed.

---

© 2026 HushAsk / HonestAsk
