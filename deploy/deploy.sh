#!/usr/bin/env bash
#
# One-command deploy for the dedicated Oracle instance.
#
#   ssh ubuntu@<ip>
#   cd ~/codeskate && ./deploy/deploy.sh
#
# Replaces the four-command dance (git pull; build; up; check) with one command
# that also does the two things a human forgets under pressure: it verifies the
# new container is actually healthy before trusting it, and if it is not, it puts
# the previous version back rather than leaving the site down.
#
# Why the rollback matters on this box specifically: there is one instance and no
# staging. A bad deploy is a live outage, and "git pull broke it, now what" at
# 1am is exactly the situation this script exists to avoid. The old image is kept
# and re-launched on any failure, so the worst case is "still on the old version"
# rather than "down".
#
# Safe to run repeatedly. If nothing changed upstream it says so and does nothing.

set -uo pipefail

# --------------------------------------------------------------------------- #
# locate the repo, regardless of where this was invoked from
# --------------------------------------------------------------------------- #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
COMPOSE_DIR="$SCRIPT_DIR"
cd "$REPO_DIR"

# Some hosts ship `docker compose` (v2), some the older `docker-compose`. Detect.
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "docker compose is not installed. Run deploy/setup-oracle.sh first." >&2
  exit 1
fi

BRANCH="${DEPLOY_BRANCH:-main}"
HEALTH_URL="http://127.0.0.1:8000/healthz"
HEALTH_TRIES=20          # ~60s: a cold start plus schema creation on first boot
HEALTH_GAP=3

c_ok()   { printf '\033[1;32m  ✓ %s\033[0m\n' "$*"; }
c_info() { printf '\033[1;36m→ %s\033[0m\n' "$*"; }
c_warn() { printf '\033[1;33m  ! %s\033[0m\n' "$*"; }
c_bad()  { printf '\033[1;31m  ✗ %s\033[0m\n' "$*"; }
step()   { printf '\n\033[1m%s\033[0m\n' "$*"; }

fail() { c_bad "$*"; echo; echo "Deploy aborted. The site is still on the previous version."; exit 1; }

# Relaunch the previous image, or rebuild the previous commit if no image is kept.
# Defined up front because bash only sees functions declared before the call.
restore_previous() {
  c_warn "restoring the previous version…"
  cd "$COMPOSE_DIR"
  if docker image inspect codeskate-app:previous >/dev/null 2>&1; then
    docker tag codeskate-app:previous codeskate-app:latest
    $DC up -d --no-build 2>/dev/null || $DC up -d
    c_ok "previous image relaunched"
  else
    c_warn "no previous image — rebuilding from commit ${PREV_COMMIT:0:8}"
    ( cd "$REPO_DIR" && git checkout --quiet "$PREV_COMMIT" )
    $DC up -d --build
  fi
}

# --------------------------------------------------------------------------- #
step "1. Checking for changes"
# --------------------------------------------------------------------------- #
[[ -d .git ]] || fail "this is not a git checkout — expected ~/codeskate to be the repo"
[[ -f "$COMPOSE_DIR/.env" ]] || fail "deploy/.env is missing — copy .env.example and fill it in"

git fetch --quiet origin "$BRANCH" || fail "could not reach the git remote"

LOCAL="$(git rev-parse HEAD)"
REMOTE="$(git rev-parse "origin/$BRANCH")"

if [[ "$LOCAL" == "$REMOTE" ]]; then
  c_ok "already up to date ($(git rev-parse --short HEAD)). Nothing to deploy."
  # Still make sure it is actually running — a no-op deploy should not hide a
  # container that died overnight.
  if curl -fsS --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
    c_ok "current version is healthy."
    exit 0
  fi
  c_warn "up to date but not responding — restarting it."
else
  c_info "new version available:"
  git --no-pager log --oneline "$LOCAL..$REMOTE" | sed 's/^/     /'
fi

# --------------------------------------------------------------------------- #
step "2. Recording the current version (for rollback)"
# --------------------------------------------------------------------------- #
PREV_COMMIT="$LOCAL"
# Tag the running image so we can relaunch this exact build if the new one fails.
if docker image inspect codeskate-app:latest >/dev/null 2>&1; then
  docker tag codeskate-app:latest codeskate-app:previous 2>/dev/null \
    && c_ok "tagged current image as rollback point" \
    || c_warn "could not tag current image — rollback will rebuild from the old commit instead"
else
  c_warn "no current image found — this looks like a first deploy, nothing to roll back to"
fi
echo "$PREV_COMMIT" > "$COMPOSE_DIR/.last-deploy"

# --------------------------------------------------------------------------- #
step "3. Pulling the new code"
# --------------------------------------------------------------------------- #
git merge --ff-only "origin/$BRANCH" \
  || fail "cannot fast-forward — the checkout has local commits. Resolve by hand."
c_ok "now at $(git rev-parse --short HEAD)"

# --------------------------------------------------------------------------- #
step "4. Building and starting"
# --------------------------------------------------------------------------- #
cd "$COMPOSE_DIR"

# --memory caps the build so it cannot starve a 1 GB box. Harmless on a larger one.
if ! $DC build --memory 400m 2>/dev/null; then
  # Older compose does not accept --memory on build; fall back.
  $DC build || { restore_previous; fail "build failed"; }
fi
c_ok "image built"

$DC up -d || { restore_previous; fail "could not start the new container"; }
c_ok "container started"

# --------------------------------------------------------------------------- #
step "5. Health check"
# --------------------------------------------------------------------------- #
healthy=0
for i in $(seq 1 "$HEALTH_TRIES"); do
  if curl -fsS --max-time 4 "$HEALTH_URL" >/dev/null 2>&1; then
    healthy=1
    c_ok "healthy after ${i} check(s)"
    break
  fi
  printf '     waiting for the app to respond (%d/%d)\r' "$i" "$HEALTH_TRIES"
  sleep "$HEALTH_GAP"
done
echo

if (( ! healthy )); then
  c_bad "the new version never became healthy. Rolling back."
  echo "     last 20 log lines from the failed container:"
  $DC logs --tail 20 app 2>/dev/null | sed 's/^/       /'
  restore_previous
  fail "rolled back to the previous version"
fi

# --------------------------------------------------------------------------- #
step "6. Done"
# --------------------------------------------------------------------------- #
# Prune the rollback tag's dangling parent layers, but keep :previous itself.
docker image prune -f >/dev/null 2>&1 || true

BASE_URL="$(grep -E '^PUBLIC_BASE_URL=' .env | cut -d= -f2- | tr -d '"')"
c_ok "deployed $(cd "$REPO_DIR" && git rev-parse --short HEAD)"
[[ -n "$BASE_URL" ]] && c_info "live at $BASE_URL"
echo
echo "If something looks wrong, roll back by hand with:"
echo "    cd ~/codeskate/deploy && docker tag codeskate-app:previous codeskate-app:latest && $DC up -d"
exit 0
