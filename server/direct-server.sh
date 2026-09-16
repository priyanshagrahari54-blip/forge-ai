#!/usr/bin/env bash
set -euo pipefail

# Forge AI direct-server bootstrap for Debian/Ubuntu.
# Runs Forge as a persistent Docker service and optionally joins a Tailscale
# private network. No public reverse proxy or Render deployment is required.

ROOT_DIR="${FORGE_ROOT:-$HOME/forge-ai}"
REPO="${FORGE_REPO:-https://github.com/priyanshagrahari54-blip/forge-ai.git}"
BRANCH="${FORGE_BRANCH:-main}"

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

cat > .env.direct-server <<'EOF'
HOST=0.0.0.0
PORT=8300
FORGE_AUTH_MODE=production
FORGE_SECURE_COOKIES=0
FORGE_DB_PATH=/data/forge/cockpit.db
FORGE_PROJECT_ID=forge
FORGE_PROJECT_ROOT=/workspace
EOF

# Keep state outside the container and restart Forge automatically.
sudo docker compose up -d --build
sudo docker compose ps

echo
 echo "Forge server is running on this machine at port 8300."
 echo "For private remote access, install Tailscale on this server and your client devices."
 echo "Do not expose port 8300 publicly unless you intentionally configure HTTPS/authentication."
