#!/usr/bin/env bash
# One-time server setup + deploy for a fresh Ubuntu 22.04/24.04 Lightsail instance (2 GB RAM recommended).
# Usage on the server:  git clone https://github.com/ignizAI/banksview.git && cd banksview/deploy/lightsail && sudo bash setup.sh
set -euo pipefail
cd "$(dirname "$0")"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi
# 2 GB swap so a 200-page statement cannot get the app killed on a small instance
if ! swapon --show | grep -q swapfile; then
  fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
  grep -q swapfile /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
if [ ! -f .env ]; then
  cp .env.example .env
  sed -i "s|^SECRET_KEY=.*|SECRET_KEY=$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')|" .env
  echo "Created .env with a fresh SECRET_KEY. Edit DOMAIN, ALLOWED_HOSTS, company details, then re-run this script."
  exit 0
fi
grep -q '^DOMAIN=.' .env || { echo "Set DOMAIN in .env"; exit 1; }
mkdir -p data
docker compose up -d --build
sleep 8
docker compose ps
echo "Health: $(curl -fsS http://127.0.0.1/health 2>/dev/null || echo 'waiting for HTTPS certificate; check again in a minute')"
