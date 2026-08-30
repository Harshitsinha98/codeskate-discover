#!/usr/bin/env bash
#
# Read-only safety check before adding CodeSkate to an instance that already runs
# something. Changes nothing except writing a baseline and backing up proxy config.
#
#   bash ~/codeskate/deploy/preflight.sh
#
# Answers one question: is there enough room, and what is the state to return to if
# this goes wrong.
#
# The concern worth taking seriously is that the Linux OOM killer does not kill the
# newest process — it kills the one with the worst badness score, usually the
# largest. On a 1 GB box that is likely to be the existing app, not the new one. So
# headroom is checked before anything is built, not after.

set -uo pipefail

STAMP="$(date +%Y%m%d-%H%M%S)"
BASELINE="$HOME/codeskate-baseline-$STAMP.txt"
BACKUP_DIR="$HOME/codeskate-proxy-backup-$STAMP"

# CodeSkate's measured peak, plus the build's transient overhead.
NEED_RUN_MB=90
NEED_BUILD_MB=350

pass=0; warn=0; fail=0
ok()   { printf '  \033[1;32mOK\033[0m    %s\n' "$*"; pass=$((pass+1)); }
note() { printf '  \033[1;33mWARN\033[0m  %s\n' "$*"; warn=$((warn+1)); }
bad()  { printf '  \033[1;31mSTOP\033[0m  %s\n' "$*"; fail=$((fail+1)); }
head_() { printf '\n\033[1;36m== %s\033[0m\n' "$*"; }

exec > >(tee "$BASELINE") 2>&1

echo "CodeSkate preflight — $(date)"
echo "Baseline written to: $BASELINE"

# --------------------------------------------------------------------------- #
head_ "Memory"
# --------------------------------------------------------------------------- #
TOTAL=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
AVAIL=$(awk '/MemAvailable/{print int($2/1024)}' /proc/meminfo)
SWAP=$(awk '/SwapTotal/{print int($2/1024)}' /proc/meminfo)
echo "  total ${TOTAL} MB · available ${AVAIL} MB · swap ${SWAP} MB"

if (( AVAIL >= NEED_BUILD_MB )); then
  ok "enough free memory to build in place (${AVAIL} MB available)"
elif (( AVAIL >= NEED_RUN_MB )) && (( SWAP >= 1024 )); then
  note "tight for the build (${AVAIL} MB) but ${SWAP} MB swap covers the spike"
  note "build when traffic is low, and use the memory-capped build command below"
elif (( AVAIL >= NEED_RUN_MB )); then
  bad "only ${AVAIL} MB free and swap is ${SWAP} MB — create swap before building"
  echo "        sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \\"
  echo "          && sudo mkswap /swapfile && sudo swapon /swapfile"
else
  bad "only ${AVAIL} MB available; CodeSkate needs ~${NEED_RUN_MB} MB to run"
  bad "use a second free instance instead — Oracle allows two"
fi

if (( SWAP == 0 )); then
  bad "no swap at all. On a 1 GB box the build will very likely be OOM-killed,"
  bad "and the kernel may kill your existing app rather than the build"
else
  ok "swap present (${SWAP} MB)"
fi

# --------------------------------------------------------------------------- #
head_ "What is already running"
# --------------------------------------------------------------------------- #
echo "  --- listening sockets ---"
sudo ss -tlnp 2>/dev/null | awk 'NR==1 || /LISTEN/' | head -20

for PORT in 8000; do
  if sudo ss -tln 2>/dev/null | grep -qE "[:.]$PORT\b"; then
    bad "port $PORT is already in use — CodeSkate needs it"
    bad "override with APP_PORT=8001 in deploy/.env"
  else
    ok "port $PORT is free"
  fi
done

for PORT in 80 443; do
  HOLDER=$(sudo ss -tlnp 2>/dev/null | grep -E "[:.]$PORT\b" | grep -oE 'users:\(\("[^"]+' | head -1 | sed 's/.*"//')
  if [[ -n "${HOLDER:-}" ]]; then
    ok "port $PORT held by '$HOLDER' — CodeSkate will sit behind it, not replace it"
    echo "$HOLDER" >> "$HOME/.codeskate-proxy"
  else
    note "nothing on port $PORT — CodeSkate can run its own Caddy (docker-compose.yml)"
  fi
done

echo "  --- processes over 40 MB ---"
ps -eo rss,comm --sort=-rss 2>/dev/null | awk 'NR==1 || $1>40000 {printf "  %6.0f MB  %s\n", $1/1024, $2}' | head -10

if command -v docker >/dev/null 2>&1; then
  ok "docker already installed — nothing new added to your firewall rules"
  echo "  --- running containers ---"
  docker ps --format '  {{.Names}}  {{.Status}}  {{.Ports}}' 2>/dev/null || true
  echo "  --- container memory ---"
  docker stats --no-stream --format '  {{.Name}}  {{.MemUsage}}' 2>/dev/null || true
else
  note "docker not installed. Installing it adds iptables rules of its own —"
  note "harmless in practice, but it is a change to networking on a live box"
fi

# --------------------------------------------------------------------------- #
head_ "Backing up proxy configuration"
# --------------------------------------------------------------------------- #
# Editing a live proxy is the second real risk. A copy costs nothing.
mkdir -p "$BACKUP_DIR"
COPIED=0
for SRC in /etc/nginx /etc/caddy; do
  if [[ -d "$SRC" ]]; then
    sudo cp -a "$SRC" "$BACKUP_DIR/" 2>/dev/null && { ok "backed up $SRC"; COPIED=1; }
  fi
done
if (( COPIED )); then
  sudo chown -R "$USER:$USER" "$BACKUP_DIR" 2>/dev/null || true
  ok "restore with: sudo cp -a $BACKUP_DIR/<nginx|caddy> /etc/"
else
  note "no /etc/nginx or /etc/caddy found — is the proxy in a container?"
fi

# --------------------------------------------------------------------------- #
head_ "Existing service health, for comparison afterwards"
# --------------------------------------------------------------------------- #
for PORT in 3001 3000 8080; do
  if sudo ss -tln 2>/dev/null | grep -qE "[:.]$PORT\b"; then
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$PORT/health" 2>/dev/null)
    [[ "$CODE" == "000" ]] && CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$PORT/" 2>/dev/null)
    ok "service on $PORT responds with HTTP $CODE — expect the same after"
  fi
done

# --------------------------------------------------------------------------- #
printf '\n\033[1;36m== Verdict\033[0m\n'
echo "  $pass ok · $warn warnings · $fail blockers"
echo

if (( fail > 0 )); then
  printf '\033[1;31m  Do not proceed yet.\033[0m Fix the STOP items above, or create a\n'
  printf '  second Always Free instance — Oracle allows two per tenancy.\n\n'
  exit 1
fi

cat <<EOF
  Safe to proceed. Build with a memory cap so the build itself cannot starve
  the existing app:

      cd ~/codeskate/deploy
      docker compose -f docker-compose.shared-proxy.yml build --memory 400m
      docker compose -f docker-compose.shared-proxy.yml up -d

  Then confirm nothing regressed:

      free -m
      curl -si localhost:3001 | head -1
      curl -s localhost:8000/healthz

  Rollback, which leaves the existing app untouched:

      cd ~/codeskate/deploy
      docker compose -f docker-compose.shared-proxy.yml down
      sudo cp -a $BACKUP_DIR/nginx /etc/ && sudo nginx -t && sudo systemctl reload nginx

EOF
