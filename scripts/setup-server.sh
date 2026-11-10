#!/usr/bin/env bash
# ============================================================================
# setup-server.sh - one-time, IDEMPOTENT setup of a fresh Ubuntu Server.
# Safe to run repeatedly.
#
# Run as a regular user (the script calls sudo itself):
#   bash scripts/setup-server.sh
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

info()  { echo -e "\n\033[1;34m==> $*\033[0m"; }
warn()  { echo -e "\033[1;33mWARNING: $*\033[0m"; }
ok()    { echo -e "\033[1;32mOK: $*\033[0m"; }

if [ "$(id -u)" -eq 0 ]; then
  echo "Do not run as root. Run as a regular user with sudo."
  exit 1
fi
sudo -v

# ---------- 1. Base packages ----------
info "System update and base packages"
sudo apt-get update
sudo apt-get upgrade -y
sudo apt-get install -y ca-certificates curl git ufw openssl cron

# ---------- 2. Docker (official repo) ----------
if command -v docker >/dev/null 2>&1; then
  ok "Docker already installed: $(docker --version)"
else
  info "Installing Docker"
  sudo install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  sudo chmod a+r /etc/apt/keyrings/docker.gpg
  CODENAME="$(. /etc/os-release && echo "${UBUNTU_CODENAME:-$VERSION_CODENAME}")"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu ${CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  sudo apt-get update
  sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  ok "Docker installed: $(docker --version)"
fi

# User in docker group (no sudo needed for docker)
if groups "$USER" | grep -qw docker; then
  ok "User $USER already in docker group"
else
  sudo usermod -aG docker "$USER"
  warn "Added $USER to docker group. After this script LOG OUT and log back in (or: newgrp docker)."
fi

# ---------- 3. Tailscale ----------
if command -v tailscale >/dev/null 2>&1; then
  ok "Tailscale already installed"
else
  info "Installing Tailscale"
  curl -fsSL https://tailscale.com/install.sh | sh
fi

if sudo tailscale status >/dev/null 2>&1; then
  ok "Tailscale already connected"
else
  info "Connecting to Tailscale - you will get a link to click"
  sudo tailscale up
fi

# ---------- 4. Firewall (UFW): zero open ports, entry only via Tailscale ----------
info "Configuring firewall (UFW)"
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on tailscale0

# Safety: if you are connected via SSH from outside Tailscale RIGHT NOW,
# keep port 22 temporarily open so you do not lock yourself out. Remove it
# after verifying SSH over Tailscale:
#   sudo ufw delete allow OpenSSH
SSH_CLIENT_IP="${SSH_CONNECTION%% *}"
if [ -n "${SSH_CONNECTION:-}" ] && [[ ! "$SSH_CLIENT_IP" =~ ^100\.(6[4-9]|[7-9][0-9]|1[0-1][0-9]|12[0-7])\. ]]; then
  warn "Detected active SSH session from outside Tailscale ($SSH_CLIENT_IP)."
  warn "Keeping port 22 open temporarily. After logging in via Tailscale, remove it:"
  warn "  sudo ufw delete allow OpenSSH"
  sudo ufw allow OpenSSH
fi

sudo ufw --force enable
ok "Firewall enabled (inbound only via Tailscale)"

# ---------- 5. Laptop: do not sleep with lid closed ----------
# Laptop as server: lid close = suspend = dead server by default.
info "Disabling sleep (laptop lid)"
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target
ok "Sleep disabled (revert: sudo systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target)"

# ---------- 6. .env file with random secrets ----------
if [ -f .env ]; then
  ok ".env already exists - not overwriting"
else
  info "Creating .env with random secrets"
  cp .env.example .env
  sed -i "s|^DB_PASSWORD=|DB_PASSWORD=$(openssl rand -hex 32)|" .env
  ok ".env created - fill in TUNNEL_TOKEN (see README). App secrets go later into env/<app>.env (deploy creates templates)"
fi

# ---------- 7. Local docker-compose.yml from example ----------
if [ -f docker-compose.yml ]; then
  ok "docker-compose.yml already exists - not overwriting"
else
  info "Creating docker-compose.yml from docker-compose.example.yml"
  cp docker-compose.example.yml docker-compose.yml
  ok "docker-compose.yml created (local file, gitignored - edit freely, e.g. enable the db service)"
fi

# ---------- 8. Working directories ----------
mkdir -p repos backups env

# ---------- 9. Cron: daily database backup at 03:30 ----------
# No-op unless the optional local Postgres is enabled (see backup.sh).
CRON_LINE="30 3 * * * $REPO_DIR/scripts/backup.sh >> $REPO_DIR/backups/backup.log 2>&1"
if crontab -l 2>/dev/null | grep -qF "scripts/backup.sh"; then
  ok "Backup cron already set"
else
  ( crontab -l 2>/dev/null || true; echo "$CRON_LINE" ) | crontab -
  ok "Added cron: backup daily at 03:30 (only when local Postgres is enabled)"
fi

chmod +x scripts/*.sh

# ---------- Done ----------
info "SETUP FINISHED"
cat <<EOF

Next steps (details in README.md):
  1. If you were added to the docker group: log out and log back in.
  2. Cloudflare Zero Trust -> Networks -> Tunnels -> Add tunnel (cloudflared),
     copy the token and paste it into .env as TUNNEL_TOKEN.
  3. (Recommended) Set CF_API_TOKEN/CF_ACCOUNT_ID/CF_TUNNEL_ID in .env -
     deploy then sets tunnel Public Hostnames from app manifests itself.
  4. Run: bash scripts/deploy.sh - on first run it creates env/<app>.env
     templates; fill in each app domain and secrets, then run deploy again.

EOF
