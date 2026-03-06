#!/bin/bash
# start-railway.sh — Single-process production entrypoint
# All Slack events + OAuth + Stripe handled by gunicorn/web.py
set -e

PORT=${PORT:-8080}
WORKERS=${WEB_WORKERS:-2}

echo "🚀 HushAsk starting on port $PORT ($WORKERS workers)..."
exec gunicorn web:web \
  --bind "0.0.0.0:$PORT" \
  --workers "$WORKERS" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
