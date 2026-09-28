#!/usr/bin/env bash
# Restores an encrypted backup made by scripts/backup.sh.
#
#   TARGET_URL          the database to restore into (PostgreSQL URL)
#   BACKUP_PASSPHRASE   the passphrase the backup was encrypted with
#
# Usage: scripts/restore_backup.sh <profitfirst-....dump.gpg>
# Replaces the app's tables in TARGET_URL with the backup's contents.
set -euo pipefail

: "${TARGET_URL:?TARGET_URL is not set}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is not set}"
backup="${1:?usage: scripts/restore_backup.sh <backup.dump.gpg>}"
TARGET_URL="${TARGET_URL/postgresql+psycopg:/postgresql:}"
TARGET_URL="${TARGET_URL/postgres:\/\//postgresql://}"

if [ "${CONFIRM:-}" != "yes" ]; then
  read -r -p "This replaces the app's data in the target database. Type yes to continue: " answer
  [ "$answer" = "yes" ] || { echo "Cancelled."; exit 1; }
fi

dump="$(mktemp)"; list="$(mktemp)"
trap 'rm -f "$dump" "$list"' EXIT
gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 \
    --decrypt --output "$dump" "$backup" 3<<<"$BACKUP_PASSPHRASE"
# Leave the public schema itself alone (never drop or re-create it); replace only its contents.
pg_restore --list "$dump" | grep -v ' SCHEMA - public ' > "$list"
pg_restore --clean --if-exists --no-owner --no-privileges --exit-on-error --single-transaction \
    --use-list="$list" --dbname="$TARGET_URL" "$dump"
echo "Restored $backup"
