#!/usr/bin/env bash
# ============================================================================
# install-cron.sh - make sure the crontab holds exactly ONE, current backup
# entry. Any old scripts/backup.sh line (e.g. a previous schedule) is
# removed and replaced. Idempotent: the crontab is only rewritten when the
# entry differs. Called by setup-server.sh and deploy.sh; manual run:
#   bash scripts/install-cron.sh
#
# Schedule: every 6 h (03:30, 09:30, 15:30, 21:30). Change it HERE only.
# ============================================================================
set -euo pipefail

cd "$(dirname "$0")/.."
REPO_DIR="$(pwd)"

BACKUP_SCHEDULE="30 3,9,15,21 * * *"
CRON_LINE="$BACKUP_SCHEDULE $REPO_DIR/scripts/backup.sh >> $REPO_DIR/backups/backup.log 2>&1"

CURRENT="$(crontab -l 2>/dev/null || true)"
OTHERS="$(printf '%s\n' "$CURRENT" | grep -vF "scripts/backup.sh" || true)"
OLD_BACKUP_LINES="$(printf '%s\n' "$CURRENT" | grep -F "scripts/backup.sh" || true)"

if [ "$OLD_BACKUP_LINES" = "$CRON_LINE" ]; then
  echo "Backup cron OK: $BACKUP_SCHEDULE"
  exit 0
fi

{ [ -n "$OTHERS" ] && printf '%s\n' "$OTHERS"; echo "$CRON_LINE"; } | crontab -

if [ -n "$OLD_BACKUP_LINES" ]; then
  echo "Backup cron updated (old entry removed): $BACKUP_SCHEDULE"
else
  echo "Backup cron added: $BACKUP_SCHEDULE"
fi
