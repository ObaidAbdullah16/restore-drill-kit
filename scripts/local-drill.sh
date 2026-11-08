#!/usr/bin/env bash
# Free drill on your laptop: two Postgres containers ("prod" and "sandbox").
#   bash scripts/local-drill.sh
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
command -v docker >/dev/null || { echo "Docker is required (Docker Desktop with WSL integration)"; exit 1; }
# shellcheck source=lib.sh
source scripts/lib.sh
ensure_restoredrill

docker compose up -d --wait
trap 'docker compose down -v >/dev/null 2>&1' EXIT
PROD="postgresql://postgres:postgres@localhost:5433/shop"
SANDBOX="postgresql://postgres:postgres@localhost:5434/postgres"

restoredrill seed --dsn "$PROD" --customers 2000
restoredrill backup --dsn "$PROD" --out backups
restoredrill drill --backup backups --sandbox-dsn "$SANDBOX" --rto-target 5m --rpo-target 24h --report reports/local
echo "Open reports/local/report.html"
