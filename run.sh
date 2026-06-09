#!/usr/bin/env bash
set -euo pipefail
cd /opt/data/apps/book-tts-web
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-9120}"
exec python3 -m uvicorn app:app --host "$HOST" --port "$PORT"
