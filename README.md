# HushAsk ⚙️

> Anonymous feedback for Slack teams — say what needs to be said.

**Product:** HushAsk
**Company:** HonestAlias
**Slash Command:** `/ha`

---

## Overview

HushAsk is a Slack-native anonymous feedback tool by HonestAlias. Team members use `/ha` to send honest, anonymous messages directly to a Slack channel — no third-party forms, no email, no friction. Sender identity is never stored or logged.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3 |
| Web Framework | Flask |
| Database | SQLite (via Python's built-in `sqlite3`) |
| Slack Integration | slack-sdk + Slack Bolt |
| Deployment | DigitalOcean Droplet (Ubuntu) |

---

## Core Product

### Slack Integration
- Slash command: `/ha #channel your message`
- Anonymous message routing — sender identity never exposed
- Messages delivered to a designated Slack channel or DM
- Slack OAuth app with proper scopes and event handling

### Backend
- Flask app to handle Slack event callbacks and slash command payloads
- SQLite for lightweight persistence (message logs, workspace configs)
- Signature verification for all incoming Slack requests

---

## Directory Structure

```
HushAsk-Core/
├── README.md               # This file
├── manifest.json           # Slack app manifest
├── app.py                  # Flask application + Slack Bolt handlers
├── database.py             # SQLite models and helpers
├── requirements.txt        # Python dependencies
├── hushask.db              # SQLite database (auto-created at runtime)
└── assets/
    ├── logo.svg            # High-res monogram icon (960×960)
    └── logo-wordmark.svg   # Full wordmark with badge (600×140)
```

---

## Setup

```bash
pip install flask slack-bolt slack-sdk
python app.py
```

### Required Environment Variables
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_SIGNING_SECRET=...
```

---

## Slack App Setup

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → Create New App → From Manifest
2. Paste contents of `manifest.json`
3. Install to your workspace
4. Copy `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` to your environment

---

## Managed by Simon ⚙️
Autonomous Technical Lead — HushAsk by HonestAlias.
