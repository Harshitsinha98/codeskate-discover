#!/usr/bin/env bash
# Exercises deploy/deploy.sh against a fake git remote and fake docker/compose/curl,
# so the control flow (up-to-date, new-version, build, health-pass, health-fail +
# rollback) is actually run rather than eyeballed. No real containers or network.
#
#   bash tests/test_deploy_script.sh
#
# Uses a BARE origin (so pushes to the checked-out branch are accepted) and a seed
# clone to populate it; the "work" clone is what deploy.sh operates on.
set -uo pipefail

SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-$SELF_DIR/../deploy/deploy.sh}"
ROOT="$(mktemp -d)"
trap 'rm -rf "$ROOT"' EXIT
mkdir -p "$ROOT/bin"

# --- fake docker + compose --------------------------------------------------
cat > "$ROOT/bin/docker" <<EOF
#!/usr/bin/env bash
case "\$1 \$2" in
  "compose version") exit 0 ;;
  "image inspect")   [[ -f "$ROOT/state/\$3" ]] && exit 0 || exit 1 ;;
  "image prune")     exit 0 ;;
  "tag"*)            mkdir -p "$ROOT/state"; touch "$ROOT/state/\$3"; exit 0 ;;
  "compose build"*)  echo "[fake build]"; [[ -f "$ROOT/BUILD_FAILS" ]] && exit 1; mkdir -p "$ROOT/state"; touch "$ROOT/state/codeskate-app:latest"; exit 0 ;;
  "compose up"*)     echo "[fake up]"; exit 0 ;;
  "compose logs"*)   echo "[fake logs] boom"; exit 0 ;;
  *)                 exit 0 ;;
esac
EOF
chmod +x "$ROOT/bin/docker"

# --- fake curl for the health check -----------------------------------------
cat > "$ROOT/bin/curl" <<EOF
#!/usr/bin/env bash
[[ -f "$ROOT/HEALTH_OK" ]] && exit 0 || exit 1
EOF
chmod +x "$ROOT/bin/curl"

# --- bare origin + seed clone -----------------------------------------------
git init -q --bare -b main "$ROOT/remote"
git init -q -b main "$ROOT/seed"; ( cd "$ROOT/seed"
  git config user.email t@t; git config user.name t
  echo v1 > app.txt
  mkdir -p deploy
  cp "$SRC" deploy/deploy.sh; chmod +x deploy/deploy.sh
  echo 'PUBLIC_BASE_URL=https://example.test' > deploy/.env
  git add -A && git commit -qm 'v1 + deploy'
  git remote add origin "$ROOT/remote"
  git push -q origin main )
git clone -q "$ROOT/remote" "$ROOT/work"
( cd "$ROOT/work" && git config user.email t@t && git config user.name t )

export PATH="$ROOT/bin:$PATH"
export HEALTH_GAP=0

run() { ( cd "$ROOT/work" && ./deploy/deploy.sh ); echo "exit=$?"; }
newcommit() { ( cd "$ROOT/seed" && echo "$1" >> app.txt && git commit -qam "$1" && git push -q origin main ); }

pass=0; fail=0
expect() { if [[ "$1" == "$2" ]]; then echo "  ok   $3"; pass=$((pass+1)); else echo "  FAIL $3 (got '$1' want '$2')"; fail=$((fail+1)); fi; }

echo "== up to date, healthy =="
mkdir -p "$ROOT/state"; touch "$ROOT/state/codeskate-app:latest" "$ROOT/HEALTH_OK"
out="$(run)"
expect "$(echo "$out" | grep -c 'already up to date')" 1 "reports up-to-date"
expect "$(echo "$out" | grep -oE 'exit=[0-9]+' | tail -1)" "exit=0" "exits clean when up to date"

echo "== new version, health passes =="
newcommit v2
out="$(run)"
expect "$(echo "$out" | grep -c 'new version available')" 1 "detects the new commit"
expect "$(echo "$out" | grep -c 'healthy after')" 1 "passes the health check"
expect "$(echo "$out" | grep -oE 'exit=[0-9]+' | tail -1)" "exit=0" "exits clean on success"
expect "$( cd "$ROOT/work" && git rev-parse HEAD )" "$( cd "$ROOT/seed" && git rev-parse HEAD )" "fast-forwarded to remote"

echo "== new version, health FAILS -> rollback =="
newcommit v3
rm -f "$ROOT/HEALTH_OK"          # health check will now fail
out="$( cd "$ROOT/work" && HEALTH_TRIES=2 ./deploy/deploy.sh; echo "exit=$?" )"
expect "$(echo "$out" | grep -cE 'restoring the previous version')" 1 "rolls back on health failure"
expect "$(echo "$out" | grep -c 'previous image relaunched')" 1 "relaunches the previous image"
expect "$(echo "$out" | grep -c 'still on the previous version')" 1 "tells the user the site is on the old version"
expect "$(echo "$out" | grep -oE 'exit=[0-9]+' | tail -1)" "exit=1" "exits non-zero on failure"

echo
echo "$pass passed, $fail failed"
[[ $fail -eq 0 ]]
