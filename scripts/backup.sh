#!/usr/bin/env bash
# ============================================================================
# backup.sh - dump the WHOLE local Postgres (all databases, including
# future apps) to backups/ (retention: 14 days). Only relevant when the
# optional local db service is enabled in docker-compose.yml. With no db
# container running, this script prints a skip message and exits 0.
# Runs daily from cron (installed by setup-server.sh), or manually:
#   bash scripts/backup.sh
#
# Restore: gunzip -c backups/pg-all-<date>.sql.gz | docker compose exec -T db psql -U postgres -d postgres
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
mkdir -p backups

if [ -z "$(docker compose ps -q db 2>/dev/null || true)" ]; then
  echo "[$(date -Is)] backup skipped: no local db container (optional Postgres not enabled)."
  exit 0
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
FILE="backups/pg-all-${STAMP}.sql.gz"

docker compose exec -T db pg_dumpall -U postgres | gzip > "$FILE"

# Remove backups older than 14 days
find backups -maxdepth 1 -name 'pg-all-*.sql.gz' -mtime +14 -delete

echo "[$(date -Is)] backup OK: $FILE ($(du -h "$FILE" | cut -f1))"
