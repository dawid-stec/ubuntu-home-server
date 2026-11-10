#!/usr/bin/env bash
# ============================================================================
# deploy.sh - fetch application code, generate services, rebuild the stack.
# Idempotent: run as often as you like, also for updates.
#
#   bash scripts/deploy.sh                full deploy of all applications
#   bash scripts/deploy.sh --changed-only rebuild only apps whose branch
#                                         got a new commit (for cron)
#
# Adding an application needs NO edit to this script: the app declares a
# homeserver.env manifest in its repo (or a fallback in manifests/ here),
# and deploy generates docker-compose.override.yml, syncs Cloudflare,
# builds, starts, and waits for healthchecks.
#
# Deploy knows nothing about databases. An app that needs one sets
# DATABASE_URL explicitly in env/<app>.env (see README).
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."

CHANGED_ONLY=0
[ "${1:-}" = "--changed-only" ] && CHANGED_ONLY=1

# Git must never prompt interactively for login/password (deploy also runs
# from cron). Missing/bad token in APP_REPOS = clear 401 error instead of
# a hanging prompt. Token: github.com/settings/tokens (Contents: Read-only).
export GIT_TERMINAL_PROMPT=0

if [ ! -f .env ]; then
  echo "Missing .env file - run first: bash scripts/setup-server.sh"
  exit 1
fi

if [ ! -f docker-compose.yml ]; then
  echo "Missing docker-compose.yml - run: bash scripts/setup-server.sh (or: cp docker-compose.example.yml docker-compose.yml)"
  exit 1
fi

# Load variables from .env
set -a; . ./.env; set +a

: "${APP_REPOS:?Set APP_REPOS in .env (see .env.example)}"

compose() { docker compose "$@"; }  # docker-compose.override.yml loads automatically

manifest_service() {  # $1 = repo directory; prints SERVICE from manifest (or nothing)
  # App repo manifest wins, then manifests/ fallback.
  local manifest=""
  if [ -f "repos/$1/homeserver.env" ]; then
    manifest="repos/$1/homeserver.env"
  elif [ -f "manifests/$1.homeserver.env" ]; then
    manifest="manifests/$1.homeserver.env"
  fi
  [ -n "$manifest" ] || return 0
  sed -n 's/^SERVICE=//p' "$manifest" | head -n 1
}

# ---------- 1. Application code ----------
# APP_REPOS format (comma between entries):  directory_name=repo_url#branch
mkdir -p repos
CHANGED_SERVICES=""
ANY_CHANGED=0
for ENTRY in ${APP_REPOS//,/ }; do
  NAME="${ENTRY%%=*}"
  REST="${ENTRY#*=}"
  URL="${REST%%#*}"
  BRANCH="${REST##*#}"
  [ "$BRANCH" = "$REST" ] && BRANCH="main"   # no "#branch" in URL

  if [ -d "repos/$NAME/.git" ]; then
    OLD_HEAD="$(git -C "repos/$NAME" rev-parse HEAD)"
    git -C "repos/$NAME" fetch origin
    NEW_HEAD="$(git -C "repos/$NAME" rev-parse "origin/$BRANCH")"
    if [ "$OLD_HEAD" != "$NEW_HEAD" ]; then
      echo "==> Updating repos/$NAME (branch: $BRANCH, new commit: ${NEW_HEAD:0:7})"
      git -C "repos/$NAME" checkout "$BRANCH"
      git -C "repos/$NAME" pull --ff-only origin "$BRANCH"
      SVC="$(manifest_service "$NAME")"
      [ -n "$SVC" ] && CHANGED_SERVICES="$CHANGED_SERVICES $SVC"
      ANY_CHANGED=1
    else
      echo "==> repos/$NAME unchanged (${OLD_HEAD:0:7})"
    fi
  else
    echo "==> Cloning $URL -> repos/$NAME"
    git clone --branch "$BRANCH" "$URL" "repos/$NAME"
    SVC="$(manifest_service "$NAME")"
    [ -n "$SVC" ] && CHANGED_SERVICES="$CHANGED_SERVICES $SVC"
    ANY_CHANGED=1
  fi
done

# ---------- 2. Generate services from manifests ----------
OLD_COMPOSE_HASH=""
[ -f docker-compose.override.yml ] && OLD_COMPOSE_HASH="$(sha256sum docker-compose.override.yml | cut -d' ' -f1)"
echo "==> Generating docker-compose.override.yml from app manifests"
python3 scripts/generate_compose.py
NEW_COMPOSE_HASH="$(sha256sum docker-compose.override.yml | cut -d' ' -f1)"

if [ "$CHANGED_ONLY" -eq 1 ] && [ "$ANY_CHANGED" -eq 0 ] && [ "$OLD_COMPOSE_HASH" = "$NEW_COMPOSE_HASH" ]; then
  echo "No new commits and no config changes - nothing to do."
  exit 0
fi
# Config-only change (manifest/env) with --changed-only = full rebuild.
if [ "$CHANGED_ONLY" -eq 1 ] && [ "$OLD_COMPOSE_HASH" != "$NEW_COMPOSE_HASH" ]; then
  CHANGED_SERVICES=""
fi

# ---------- 3. Cloudflare Tunnel (Public Hostnames via API) ----------
echo "==> Syncing Cloudflare"
python3 scripts/cloudflare_sync.py || echo "    WARNING: Cloudflare sync failed (apps run locally; check CF_* in .env)."

# ---------- 4. Build and start ----------
echo "==> Pulling external images"
docker compose pull --ignore-buildable 2>/dev/null || docker compose pull cloudflared

echo "==> Building and restarting services"
if [ "$CHANGED_ONLY" -eq 1 ] && [ -n "${CHANGED_SERVICES# }" ]; then
  # shellcheck disable=SC2086
  compose up -d --build $CHANGED_SERVICES
  compose up -d cloudflared
else
  compose up -d --build
fi

# ---------- 5. Verify (healthchecks from manifests) ----------
FAILED=0
HEALTH_SERVICES="$(awk -F'\t' '$4 == "true" {print $2}' .deploy/apps.tsv)"
for SVC in $HEALTH_SERVICES; do
  echo "==> Waiting for healthcheck $SVC (max ~60 s)..."
  STATUS=""
  for _ in $(seq 1 30); do
    CID="$(compose ps -q "$SVC" 2>/dev/null || true)"
    if [ -n "$CID" ]; then
      STATUS="$(docker inspect --format='{{.State.Health.Status}}' "$CID" 2>/dev/null || echo unknown)"
      [ "$STATUS" = "healthy" ] && break
    fi
    sleep 2
  done
  if [ "$STATUS" = "healthy" ]; then
    echo "    $SVC: healthy"
  else
    echo "    WARNING: $SVC is not 'healthy' (status: ${STATUS:-missing})"
    echo "    Logs: docker compose logs $SVC --tail 100"
    FAILED=1
  fi
done

compose ps

if [ "$FAILED" -eq 0 ]; then
  echo ""
  echo "DEPLOY OK - all services with a healthcheck are healthy."
  awk -F'\t' '$5 != "" {print "  https://" $5 "  (" $2 ")"}' .deploy/apps.tsv
else
  echo ""
  echo "DEPLOY WITH PROBLEMS - see above, check logs."
  exit 1
fi
