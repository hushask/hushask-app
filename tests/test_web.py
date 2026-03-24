"""
tests/test_web.py — Flask web layer tests for HushAsk.

Tests XSS escaping, URL validation, subdomain redirect, and rate limiting.
No real Slack or Stripe calls are made.
"""
import os
import uuid
import pytest
from unittest.mock import patch, MagicMock

# Must set env vars before importing web or app modules
os.environ.setdefault("HASH_SALT", "test-salt-for-testing")
os.environ.setdefault("SLACK_SIGNING_SECRET", "test-secret")
os.environ.setdefault("SLACK_CLIENT_ID", "test-client-id")
os.environ.setdefault("SLACK_CLIENT_SECRET", "test-client-secret")
os.environ.setdefault("NOTION_ENCRYPTION_KEY", "")
os.environ.setdefault("STRIPE_SECRET_KEY", "")
os.environ.setdefault("STRIPE_PRO_PRICE_ID", "")
os.environ.setdefault("STRIPE_WEBHOOK_SECRET", "")
os.environ.setdefault("NOTION_CLIENT_ID", "")
os.environ.setdefault("NOTION_CLIENT_SECRET", "")

import database


@pytest.fixture(autouse=True)
def use_temp_db(tmp_path):
    db_file = str(tmp_path / "test.db")
    original = database.DB_PATH
    database.DB_PATH = db_file
    database.init_db()
    yield
    database.DB_PATH = original


@pytest.fixture(scope="module")
def flask_client():
    """Create a Flask test client with Slack Bolt app mocked out."""
    import sys

    # Remove any previously cached app/web modules so mocks apply cleanly
    for mod in list(sys.modules.keys()):
        if mod in ("app", "web"):
            del sys.modules[mod]

    with patch("slack_bolt.App", MagicMock()), \
         patch("slack_bolt.oauth.oauth_settings.OAuthSettings", MagicMock()), \
         patch("slack_bolt.adapter.flask.SlackRequestHandler", MagicMock()):
        import web as web_module
        web_module.web.config["TESTING"] = True
        client = web_module.web.test_client()
        yield client


# ── 7: /notion/error escapes XSS in reason parameter ─────────────────────────

def test_notion_error_escapes_xss(flask_client):
    response = flask_client.get("/notion/error?reason=<script>alert(1)</script>")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    # Raw script tag must NOT appear
    assert "<script>alert(1)</script>" not in body, (
        "XSS payload must be escaped in /notion/error response"
    )
    # Escaped form must appear
    assert "&lt;script&gt;" in body, (
        "Escaped &lt;script&gt; must appear in /notion/error response"
    )


# ── 8: /notion/connected rejects non-Notion URLs ─────────────────────────────

def test_notion_connected_rejects_non_notion_url(flask_client):
    response = flask_client.get("/notion/connected?db_url=https://evil.com/steal")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert 'href="https://evil.com/steal"' not in body, (
        "Non-Notion db_url must not appear as a link in the response"
    )


# ── 9: /notion/connected allows real Notion URLs ─────────────────────────────

def test_notion_connected_allows_notion_url(flask_client):
    notion_url = "https://notion.so/abc123def456"
    response = flask_client.get(f"/notion/connected?db_url={notion_url}")
    assert response.status_code == 200
    body = response.data.decode("utf-8")
    assert f'href="{notion_url}"' in body, (
        f"Notion URL '{notion_url}' should appear as a link in the response"
    )


# ── 10: api.hushask.com subdomain redirects to hushask.com ───────────────────

def test_api_subdomain_redirect(flask_client):
    response = flask_client.get(
        "/health",
        headers={"Host": "api.hushask.com"}
    )
    assert response.status_code == 301, (
        f"api.hushask.com requests should 301 redirect, got {response.status_code}"
    )
    location = response.headers.get("Location", "")
    assert location == "https://hushask.com/health", (
        f"Redirect Location should be 'https://hushask.com/health', got '{location}'"
    )


# ── 11: check_checkout_rate enforces rate limit ───────────────────────────────

def test_checkout_rate_limit():
    # Use a unique team_id per test run to avoid cross-test state pollution
    team_id = f"T_RATE_{uuid.uuid4().hex[:8]}"

    # First call: should be allowed
    first = database.check_checkout_rate(team_id, window_seconds=60)
    assert first is True, "First checkout attempt should be allowed"

    # Second call within window: should be rate limited
    second = database.check_checkout_rate(team_id, window_seconds=60)
    assert second is False, "Second checkout attempt within 60s should be rate limited"
