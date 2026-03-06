#!/bin/bash
# start-railway.sh — Production entrypoint for Railway/Render
set -e

PORT=${PORT:-8080}

echo "🤖 Starting Slack bot (Socket Mode, background)..."
python -u app.py &
BOT_PID=$!
echo "   Bot PID: $BOT_PID"

sleep 2

echo "🌐 Starting web server on port $PORT (gunicorn, foreground)..."
exec gunicorn web:web \
  --bind "0.0.0.0:$PORT" \
  --workers 2 \
  --timeout 60 \
  --access-logfile - \
  --error-logfile -
