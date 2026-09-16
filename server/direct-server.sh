#!/usr/bin/env bash
set -euo pipefail

# Forge AI direct-server bootstrap for Debian/Ubuntu.
# Runs Forge as a persistent Docker service. Optional Tailscale can provide
# private remote access without making Forge a public website.

ROOT_DIR="${FORGE_ROOT:-$HOME/forge-ai}"
REPO="${FORGE_REPO:-https://github.com/priyanshagrahari54-blip/forge-ai.git}"
BRANCH="${FORGE_BRANCH:-main}"
SERVICE_SRC="$ROOT_DIR/server/forge-direct.service"
SERVICE_DST="/etc/systemd/system/forge-direct.service"

if [[ "${EUID}" -eq 0 ]]; then
  echo "Run this script as a normal user with sudo access, not as root."
  exit 1
fi

sudo apt-get update
sudo apt-get install -y ca-certificates curl git

if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Docker was installed. Log out/in once so your user receives docker group access."
fi

if [[ ! -d "$ROOT_DIR/.git" ]]; then
  git clone --branch "$BRANCH" "$REPO" "$ROOT_DIR"
else
  git -C "$ROOT_DIR" fetch origin
  git -C "$ROOT_DIR" checkout "$BRANCH"
  git -C "$ROOT_DIR" pull --ff-only origin "$BRANCH"
fi

cd "$ROOT_DIR"
mkdir -p workspace

sudo install -m 0644 "$SERVICE_SRC" "$SERVICE_DST"
sudo systemctl daemon-reload
sudo systemctl enable docker.service
sudo systemctl enable --now forge-direct.service

sudo docker compose ps

echo
echo "Forge direct server is installed and enabled at boot."
echo "Forge listens on port 8300 on this server."
echo "For private remote access, install Tailscale on this server and your client devices."
echo "Do not expose port 8300 publicly unless you intentionally configure HTTPS/authentication."
