#!/bin/bash
# start-railway.sh — Single-process production entrypoint
# All Slack events + OAuth + Stripe handled by gunicorn/web.py
# NOTION_ENCRYPTION_KEY should be set in Railway env for token encryption
# Generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
set -e

PORT=${PORT:-8080}
WORKERS=${WEB_WORKERS:-2}

mkdir -p /data

echo "🚀 HushAsk starting on port $PORT ($WORKERS workers)..."
exec gunicorn web:web \
  --bind "0.0.0.0:$PORT" \
  --workers "$WORKERS" \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
