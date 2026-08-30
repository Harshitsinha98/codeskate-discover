#!/usr/bin/env bash
#
# Bootstrap an Oracle Cloud Always Free instance to run CodeSkate.
# Safe to re-run: every step checks before acting.
#
#   curl -fsSL https://raw.githubusercontent.com/Harshitsinha98/codeskate-discover/main/deploy/setup-oracle.sh | bash
#
# Handles the two things that trip up nearly every Oracle Cloud deployment:
#
#   1. Oracle's images ship iptables rules that drop everything except SSH. Opening
#      ports in the cloud console alone is not enough — the instance itself still
#      refuses the traffic, and it looks exactly like a broken app.
#   2. A 1 GB instance with no swap will have the OOM killer terminate a container
#      mid-build. 2 GB of swap makes the difference between "slow" and "dead".

set -euo pipefail

REPO_URL="https://github.com/Harshitsinha98/codeskate-discover.git"
APP_DIR="${APP_DIR:-$HOME/codeskate}"
SWAP_SIZE="${SWAP_SIZE:-2G}"

log()  { printf '\n\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m  ok\033[0m %s\n' "$*"; }

if [[ $EUID -eq 0 ]]; then
  warn "Running as root. Prefer the default user (ubuntu/opc) — this script uses sudo where needed."
fi

# --------------------------------------------------------------------------- #
log "Detecting the platform"
# --------------------------------------------------------------------------- #
. /etc/os-release
ARCH="$(uname -m)"
MEM_MB="$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)"
echo "  OS:     $PRETTY_NAME"
echo "  Arch:   $ARCH"
echo "  Memory: ${MEM_MB} MB"

case "$ID" in
  ubuntu|debian) PKG=apt ;;
  ol|oracle|rhel|centos|almalinux|rocky) PKG=dnf ;;
  *) warn "Unrecognised distribution '$ID' — assuming dnf"; PKG=dnf ;;
esac

if (( MEM_MB < 900 )); then
  warn "Under 1 GB of RAM. This will be tight; swap below is essential."
fi
if (( MEM_MB < 1600 )); then
  echo
  warn "Small instance detected. Do NOT run Postgres here — keep the database on"
  warn "Neon's free tier. The app itself peaks around 71 MB and is fine."
fi

# --------------------------------------------------------------------------- #
log "Swap"
# --------------------------------------------------------------------------- #
if swapon --show | grep -q '/swapfile'; then
  ok "swap already configured ($(swapon --show=SIZE --noheadings | tr -d ' ' | head -1))"
else
  sudo fallocate -l "$SWAP_SIZE" /swapfile 2>/dev/null || \
    sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  # Prefer RAM but allow swapping rather than killing a process outright.
  sudo sysctl -q -w vm.swappiness=20
  grep -q 'vm.swappiness' /etc/sysctl.conf || echo 'vm.swappiness=20' | sudo tee -a /etc/sysctl.conf >/dev/null
  ok "created ${SWAP_SIZE} swap"
fi

# --------------------------------------------------------------------------- #
log "Docker"
# --------------------------------------------------------------------------- #
if command -v docker >/dev/null 2>&1; then
  ok "docker present ($(docker --version | cut -d, -f1))"
else
  if [[ $PKG == apt ]]; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq ca-certificates curl gnupg git
    sudo install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
      | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
    sudo chmod a+r /etc/apt/keyrings/docker.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
      | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
    sudo apt-get update -qq
    sudo apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  else
    sudo dnf install -y -q git dnf-utils
    sudo dnf config-manager --add-repo https://download.docker.com/linux/centos/docker-ce.repo 2>/dev/null || true
    sudo dnf install -y -q docker-ce docker-ce-cli containerd.io docker-compose-plugin
  fi
  sudo systemctl enable --now docker
  ok "docker installed"
fi

if ! groups | grep -qw docker; then
  sudo usermod -aG docker "$USER"
  warn "Added $USER to the docker group. Log out and back in, or run: newgrp docker"
fi

# --------------------------------------------------------------------------- #
log "Opening ports 80 and 443 on the instance"
# --------------------------------------------------------------------------- #
# This is the step people miss. Oracle's images drop inbound traffic locally, so
# the cloud-console security list is necessary but not sufficient.
if command -v firewall-cmd >/dev/null 2>&1 && sudo firewall-cmd --state >/dev/null 2>&1; then
  sudo firewall-cmd --permanent --add-service=http  >/dev/null
  sudo firewall-cmd --permanent --add-service=https >/dev/null
  sudo firewall-cmd --reload >/dev/null
  ok "firewalld: http and https allowed"
else
  for PORT in 80 443; do
    if sudo iptables -C INPUT -p tcp --dport "$PORT" -j ACCEPT 2>/dev/null; then
      ok "iptables: port $PORT already allowed"
    else
      # Insert ahead of the catch-all REJECT that Oracle's images place last.
      sudo iptables -I INPUT 1 -p tcp --dport "$PORT" -m conntrack --ctstate NEW -j ACCEPT
      ok "iptables: opened port $PORT"
    fi
  done
  if [[ $PKG == apt ]]; then
    sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq iptables-persistent >/dev/null 2>&1 || true
    sudo netfilter-persistent save >/dev/null 2>&1 || sudo sh -c 'iptables-save > /etc/iptables/rules.v4' || true
  else
    sudo sh -c 'iptables-save > /etc/sysconfig/iptables' 2>/dev/null || true
  fi
  ok "iptables rules persisted across reboot"
fi

# --------------------------------------------------------------------------- #
log "Fetching the application"
# --------------------------------------------------------------------------- #
if [[ -d "$APP_DIR/.git" ]]; then
  git -C "$APP_DIR" pull --ff-only
  ok "updated $APP_DIR"
else
  git clone --depth 1 "$REPO_URL" "$APP_DIR"
  ok "cloned into $APP_DIR"
fi

cd "$APP_DIR/deploy"
if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env
  warn "Created deploy/.env from the example — it still needs your values."
fi

# --------------------------------------------------------------------------- #
PUBLIC_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || echo '<your-ip>')"
cat <<EOF

$(printf '\033[1;32m')Instance is ready.$(printf '\033[0m')

Two things left, and neither can be scripted from inside the instance.

$(printf '\033[1;36m')1. Open 80 and 443 in the Oracle console$(printf '\033[0m')
   Networking → Virtual Cloud Networks → your VCN → Subnet → Security List →
   Add Ingress Rules:
       Source 0.0.0.0/0   TCP   destination port 80
       Source 0.0.0.0/0   TCP   destination port 443
   Without this, Let's Encrypt cannot reach the instance and no certificate is
   issued. The local firewall is already handled.

$(printf '\033[1;36m')2. Fill in deploy/.env$(printf '\033[0m')
       nano $APP_DIR/deploy/.env

   Your public IP is: $PUBLIC_IP
   With no domain of your own, use:
       SITE_DOMAIN=$PUBLIC_IP.sslip.io
       PUBLIC_BASE_URL=https://$PUBLIC_IP.sslip.io

   Then add this exact redirect URI to your Google OAuth client:
       https://\$SITE_DOMAIN/api/auth/google/callback

$(printf '\033[1;36m')Then start it$(printf '\033[0m')
       cd $APP_DIR/deploy && docker compose up -d --build
       docker compose logs -f app

The first build takes several minutes on a small instance — that is the swap
earning its place. Certificates arrive within a minute of the first HTTPS request.

EOF
