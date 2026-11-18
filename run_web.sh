#!/usr/bin/env bash
# ==============================================================================
# 1-Click Launcher for Web Annotation Platform & Backend Services
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "=========================================================="
echo " Starting Web Annotation Platform Services"
echo " Working directory: $SCRIPT_DIR"
echo "=========================================================="

if command -v docker &>/dev/null && docker compose version &>/dev/null; then
    docker compose up -d
elif command -v docker-compose &>/dev/null; then
    docker-compose up -d
else
    echo "Error: docker compose not found. Please ensure Docker is installed."
    exit 1
fi

echo ""
echo "Waiting for services to become healthy..."
sleep 3

PORT="8080"
if [ -f .env ]; then
    ENV_PORT=$(grep -E "^CONTROLLER_PORT=" .env | cut -d '=' -f2 | tr -d ' "' || true)
    if [ -n "$ENV_PORT" ]; then
        PORT="$ENV_PORT"
    fi
fi

HOST_IP=$(hostname -I 2>/dev/null | awk '{print $1}' || echo "localhost")

echo "=========================================================="
echo " Web Annotation Platform is running!"
echo " Local Access:    http://localhost:${PORT}/dashboard"
echo " Network Access:  http://${HOST_IP}:${PORT}/dashboard"
echo " API Docs:        http://localhost:${PORT}/docs"
echo "=========================================================="

