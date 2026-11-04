#!/usr/bin/env bash
# Deploy the latest main: run on the server from deploy/lightsail
set -euo pipefail
cd "$(dirname "$0")"
git pull --ff-only
docker compose exec -T app python -m app.cli backup || true   # safety copy before the restart
docker compose up -d --build
docker image prune -f >/dev/null
docker compose ps
