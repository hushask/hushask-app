# ── HushAsk — Railway/Render Production Image ────────────────────────────────
# Runs two processes: Slack Socket Mode bot (background) + Gunicorn web server
FROM python:3.11-slim

WORKDIR /app

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Python deps
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App source (secrets injected at runtime via env vars — not baked in)
COPY app.py database.py web.py crypto.py start-railway.sh ./
COPY index.html privacy.html terms.html pricing.html faq.html ./
COPY robots.txt sitemap.xml favicon.ico ./
COPY blog/ ./blog/
COPY help/ ./help/
COPY assets/ ./assets/

RUN chmod +x start-railway.sh

# Railway injects PORT at runtime
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8080}/health || exit 1

CMD ["bash", "start-railway.sh"]
