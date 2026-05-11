"""
analytics.py — privacy-first server-side page-view logger.

What gets captured per request:
    timestamp (UTC) | path | referrer host (no path / query) | country (CF-IPCountry)
    device_type    | browser | visitor_hash (daily-rotating, cookie-less)

What we deliberately do NOT capture:
    IP address, raw user-agent string, cookies, query strings, session IDs, any PII.

The visitor_hash rotates every UTC day (sha256 of date|country|UA|HASH_SALT),
so unique-visitor counts work without a tracking cookie. Bots, admin routes,
webhooks, and static assets are skipped.

Schema is created idempotently — calling init_analytics_db() is safe to repeat.
"""

import hashlib
import os
import re
import sqlite3
from datetime import datetime, timezone

from flask import request

from database import get_conn

_HASH_SALT = os.environ.get("HASH_SALT", "")

_BOT_RE = re.compile(
    r"(bot|crawl|spider|slurp|wget|curl|python-requests|httpclient|"
    r"headless|preview|fetch|monitor|uptimerobot|axios|okhttp|"
    r"lighthouse|googleother|google-inspectiontool|pingdom|"
    r"ahrefs|semrush|mj12|dotbot|petalbot|bingpreview|chatgpt-user|"
    r"gptbot|claudebot|claude-web)",
    re.IGNORECASE,
)

_SKIP_PREFIXES = (
    "/admin/", "/slack/", "/stripe/", "/notion/",
    "/upgrade", "/health",
)
_SKIP_PATHS = {"/favicon.ico", "/robots.txt", "/sitemap.xml"}
_STATIC_EXTS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg",
    ".ico", ".webp", ".woff", ".woff2", ".ttf", ".map", ".webmanifest",
)


def init_analytics_db():
    """Idempotent — creates the page_views table on first run."""
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS page_views (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT    DEFAULT (datetime('now')),
                path          TEXT    NOT NULL,
                referrer_host TEXT    DEFAULT '',
                country       TEXT    DEFAULT '',
                device_type   TEXT    DEFAULT '',
                browser       TEXT    DEFAULT '',
                visitor_hash  TEXT    NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_pv_ts   ON page_views(ts);
            CREATE INDEX IF NOT EXISTS idx_pv_path ON page_views(path);
        """)


def _should_skip(path: str, ua: str) -> bool:
    if not path or path in _SKIP_PATHS:
        return True
    if path.startswith(_SKIP_PREFIXES):
        return True
    lower = path.lower()
    if lower.endswith(_STATIC_EXTS):
        return True
    if not ua or _BOT_RE.search(ua):
        return True
    return False


def _referrer_host(ref: str) -> str:
    """Strip everything except the host. No path, no query, no port."""
    if not ref:
        return ""
    try:
        from urllib.parse import urlparse
        h = urlparse(ref).netloc.lower()
        h = h.split("@")[-1].split(":")[0]
        return h[:128]
    except Exception:
        return ""


def _device_type(ua_string: str) -> str:
    s = (ua_string or "").lower()
    if "ipad" in s or "tablet" in s:
        return "tablet"
    if "mobi" in s or "android" in s or "iphone" in s:
        return "mobile"
    return "desktop"


def _browser_name(ua_string: str) -> str:
    s = (ua_string or "").lower()
    # Order matters — Edge/Opera UAs also contain "chrome"
    if "edg/" in s or "edge" in s:
        return "Edge"
    if "opr/" in s or "opera" in s:
        return "Opera"
    if "firefox" in s:
        return "Firefox"
    if "chrome" in s and "chromium" not in s:
        return "Chrome"
    if "safari" in s:
        return "Safari"
    return "Other"


def _visitor_hash(ua: str, country: str) -> str:
    """Cookie-less daily-rotating visitor identity. Same person on day N+1 hashes differently."""
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    blob = f"{day}|{country}|{ua}|{_HASH_SALT}".encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:32]


def record_page_view():
    """Flask before_request hook. Silent on errors — never breaks a page render."""
    try:
        path = request.path or "/"
        ua_string = request.headers.get("User-Agent", "")
        if _should_skip(path, ua_string):
            return
        country = (request.headers.get("CF-IPCountry") or "").upper()[:4]
        if country == "XX":  # CF code for unknown / anonymizer
            country = ""
        ref_host = _referrer_host(request.headers.get("Referer", ""))
        device   = _device_type(ua_string)
        browser  = _browser_name(ua_string)
        vh       = _visitor_hash(ua_string, country)

        with get_conn() as conn:
            conn.execute(
                "INSERT INTO page_views "
                "(path, referrer_host, country, device_type, browser, visitor_hash) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (path[:512], ref_host, country, device, browser, vh),
            )
    except Exception as e:
        print(f"[analytics] record_page_view failed: {e}")


def get_summary(days: int = 28) -> dict:
    """Aggregate stats for /admin/analytics. Single SQLite connection, one read per panel."""
    days = max(1, min(int(days), 365))
    with get_conn() as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()

        cur.execute(f"""
            SELECT COUNT(*) AS pageviews,
                   COUNT(DISTINCT visitor_hash) AS visitors
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
        """)
        totals = dict(cur.fetchone() or {})

        cur.execute(f"""
            SELECT path, COUNT(*) AS views, COUNT(DISTINCT visitor_hash) AS uniques
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
            GROUP BY path
            ORDER BY views DESC
            LIMIT 20
        """)
        top_pages = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT referrer_host AS host, COUNT(*) AS views
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
              AND referrer_host <> ''
              AND referrer_host NOT LIKE '%hushask.com%'
            GROUP BY referrer_host
            ORDER BY views DESC
            LIMIT 20
        """)
        referrers = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT COALESCE(NULLIF(country, ''), 'Unknown') AS country, COUNT(*) AS views
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
            GROUP BY country
            ORDER BY views DESC
            LIMIT 20
        """)
        countries = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT device_type, COUNT(*) AS views
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
            GROUP BY device_type
            ORDER BY views DESC
        """)
        devices = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT browser, COUNT(*) AS views
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
            GROUP BY browser
            ORDER BY views DESC
        """)
        browsers = [dict(r) for r in cur.fetchall()]

        cur.execute(f"""
            SELECT date(ts) AS day,
                   COUNT(*) AS views,
                   COUNT(DISTINCT visitor_hash) AS visitors
            FROM page_views
            WHERE datetime(ts) >= datetime('now', '-{days} days')
            GROUP BY date(ts)
            ORDER BY day
        """)
        daily = [dict(r) for r in cur.fetchall()]

    return {
        "window_days": days,
        "pageviews": totals.get("pageviews", 0) or 0,
        "visitors": totals.get("visitors", 0) or 0,
        "top_pages": top_pages,
        "referrers": referrers,
        "countries": countries,
        "devices": devices,
        "browsers": browsers,
        "daily": daily,
        "as_of": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
