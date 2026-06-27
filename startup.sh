#!/bin/bash
# startup.sh — Render post-deploy startup script
# Runs database migrations then starts the API server
# Used as buildCommand override for the web service when migrations are needed

set -e

echo "=== AMR-Sentinel startup ==="
echo "Running Alembic migrations..."
python -m alembic upgrade head

echo "Migrations complete."
echo "Starting FastAPI..."
exec python -m uvicorn amr_sentinel.api.main:app --host 0.0.0.0 --port $PORT