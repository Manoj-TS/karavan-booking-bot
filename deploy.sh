#!/usr/bin/env bash
# Deploy/run Karavan Booking Bot on a VPS via Docker.
#
# First run: installs Docker if missing, generates a .env with a random
# BB_SECRET_KEY and a Basic Auth login (the whole app is otherwise wide open
# to anyone with the URL), then builds and starts the container.
# Later runs (e.g. after `git pull`): just rebuilds and restarts it.
set -euo pipefail

cd "$(dirname "$0")"

if [ -d .git ]; then
  echo "Pulling latest code..."
  git pull --ff-only || echo "  (skipped: not on a fast-forwardable branch, or no remote)"
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker not found -- installing..."
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
fi

if [ ! -f .env ]; then
  echo "No .env found -- generating one..."
  cp .env.example .env
  SECRET=$(openssl rand -hex 32)
  AUTH_PASS=$(openssl rand -hex 12)
  sed -i "s|^BB_SECRET_KEY=.*|BB_SECRET_KEY=${SECRET}|" .env
  sed -i "s|^BB_PORT=.*|BB_PORT=6000|" .env
  sed -i "s|^BB_AUTH_USER=.*|BB_AUTH_USER=admin|" .env
  sed -i "s|^BB_AUTH_PASS=.*|BB_AUTH_PASS=${AUTH_PASS}|" .env
  echo
  echo "  Generated login -> user: admin   password: ${AUTH_PASS}"
  echo "  (also saved in .env -- save this password now, it won't be shown again)"
  echo
fi

PORT=$(grep -E '^BB_PORT=' .env | cut -d= -f2)
PORT=${PORT:-8000}

if command -v ufw >/dev/null 2>&1; then
  ufw allow "${PORT}/tcp" >/dev/null 2>&1 || true
fi

docker compose up -d --build

IP=$(curl -s -4 ifconfig.me 2>/dev/null || echo "<your-server-ip>")
echo
echo "Karavan Booking Bot is up: http://${IP}:${PORT}"
echo "Logs:    docker compose logs -f"
echo "Stop:    docker compose down"
echo "Restart: docker compose restart"
