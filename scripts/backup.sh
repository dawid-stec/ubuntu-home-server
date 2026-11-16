#!/usr/bin/env bash
# ============================================================================
# backup.sh - dump the WHOLE local Postgres (all databases, including
# future apps) to backups/ (retention: 30 days). Only relevant when the
# optional local db service is enabled in docker-compose.yml. With no db
# container running, this script prints a skip message and exits 0.
# Runs every 6 h from cron (installed by setup-server.sh), or manually:
#   bash scripts/backup.sh
#
# Off-site copy (optional): when R2_BUCKET is set in .env, the dump is also
# uploaded to Cloudflare R2 (S3-compatible) using the rclone Docker image -
# nothing to install on the host. If BACKUP_ENCRYPTION_PASSPHRASE is set,
# the uploaded copy is GPG-encrypted (AES256). See README "Backups".
#
# Restore: gunzip -c backups/pg-all-<date>.sql.gz | docker compose exec -T db psql -U postgres -d postgres
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups

# Load .env (R2_* and BACKUP_* settings). Missing .env = local backup only.
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

if [ -z "$(docker compose ps -q db 2>/dev/null || true)" ]; then
  echo "[$(date -Is)] backup skipped: no local db container (optional Postgres not enabled)."
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="backups/pg-all-${STAMP}.sql.gz"

docker compose exec -T db pg_dumpall -U postgres | gzip > "$FILE"

# Remove local backups older than 30 days (a month)
find backups -maxdepth 1 -name 'pg-all-*.sql.gz' -mtime +29 -delete

echo "[$(date -Is)] backup OK: $FILE ($(du -h "$FILE" | cut -f1))"

# ---------- Off-site copy to Cloudflare R2 ----------
if [ -z "${R2_BUCKET:-}" ]; then
  exit 0
fi

: "${R2_ACCESS_KEY_ID:?R2_BUCKET is set but R2_ACCESS_KEY_ID is missing in .env}"
: "${R2_SECRET_ACCESS_KEY:?R2_BUCKET is set but R2_SECRET_ACCESS_KEY is missing in .env}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID:-${CF_ACCOUNT_ID:-}}"
: "${R2_ACCOUNT_ID:?Set R2_ACCOUNT_ID (or CF_ACCOUNT_ID) in .env}"
R2_PREFIX="${R2_PREFIX:-postgres}"
R2_RETENTION_DAYS="${R2_RETENTION_DAYS:-30}"
RCLONE_IMAGE="rclone/rclone:1.68.2"

UPLOAD="$FILE"
TMP_ENC=""
cleanup() { [ -n "$TMP_ENC" ] && rm -f "$TMP_ENC"; }
trap cleanup EXIT

if [ -n "${BACKUP_ENCRYPTION_PASSPHRASE:-}" ]; then
  TMP_ENC="${FILE}.gpg"
  # Passphrase via fd 3, so it never appears in the process list.
  gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 \
      --symmetric --cipher-algo AES256 -o "$TMP_ENC" "$FILE" \
      3<<<"$BACKUP_ENCRYPTION_PASSPHRASE"
  UPLOAD="$TMP_ENC"
fi

# rclone configured entirely via env vars (remote name "r2").
# NO_CHECK_BUCKET: a bucket-scoped API token cannot list/create buckets.
rclone() {
  docker run --rm --network host \
    -e RCLONE_CONFIG_R2_TYPE=s3 \
    -e RCLONE_CONFIG_R2_PROVIDER=Cloudflare \
    -e RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" \
    -e RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    -e RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com" \
    -e RCLONE_CONFIG_R2_NO_CHECK_BUCKET=true \
    -v "$PWD/backups:/backups:ro" \
    "$RCLONE_IMAGE" "$@"
}

DEST="r2:${R2_BUCKET}/${R2_PREFIX}/$(basename "$UPLOAD")"
if rclone copyto "/backups/$(basename "$UPLOAD")" "$DEST"; then
  echo "[$(date -Is)] R2 upload OK: $DEST"
else
  echo "[$(date -Is)] R2 upload FAILED: $DEST (local backup kept)" >&2
  exit 1
fi

# Remote retention: delete objects older than R2_RETENTION_DAYS (0 = keep all)
if [ "$R2_RETENTION_DAYS" -gt 0 ]; then
  rclone delete "r2:${R2_BUCKET}/${R2_PREFIX}" --min-age "${R2_RETENTION_DAYS}d" \
    || echo "[$(date -Is)] WARNING: R2 retention cleanup failed" >&2
fi
