#!/usr/bin/env bash
# Backs up the application data (schema "public") and proves the backup restores.
#
#   SOURCE_URL          the production database (PostgreSQL URL)
#   RESTORE_URL         an empty scratch database the backup is test-restored into
#   BACKUP_PASSPHRASE   encrypts the backup file (AES-256); keep it outside GitHub too
#
# Usage: scripts/backup.sh <output-dir>
# Writes <output-dir>/profitfirst-<UTC timestamp>.dump.gpg, and fails without
# writing it if the test restore is incomplete.
set -euo pipefail

: "${SOURCE_URL:?SOURCE_URL is not set}"
: "${RESTORE_URL:?RESTORE_URL is not set}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE is not set}"
out_dir="${1:?usage: scripts/backup.sh <output-dir>}"
mkdir -p "$out_dir"

# postgresql+psycopg://... (the app's SQLAlchemy form) -> postgresql://... for libpq tools
SOURCE_URL="${SOURCE_URL/postgresql+psycopg:/postgresql:}"
SOURCE_URL="${SOURCE_URL/postgres:\/\//postgresql://}"

TABLES="organizations users entities runs reviews distribution_plans plan_events"
dump="$out_dir/profitfirst-$(date -u +%Y%m%dT%H%M%SZ).dump"
trap 'rm -f "$dump"' EXIT  # never leave an unencrypted copy behind

# Only the app's own schema: hosted databases (e.g. Supabase) add schemas and
# roles of their own that neither belong in the backup nor restore elsewhere.
pg_dump --format=custom --schema=public --no-owner --no-privileges --dbname="$SOURCE_URL" --file="$dump"
# The public schema already exists in every database, so skip its CREATE.
list="$(mktemp)"; trap 'rm -f "$dump" "$list"' EXIT
pg_restore --list "$dump" | grep -v ' SCHEMA - public ' > "$list"
pg_restore --no-owner --no-privileges --exit-on-error --use-list="$list" --dbname="$RESTORE_URL" "$dump"

row_counts() {
  for table in $TABLES; do
    printf '%s=%s\n' "$table" "$(psql "$1" -X -A -t -v ON_ERROR_STOP=1 -c "SELECT count(*) FROM public.$table")"
  done
}
restored="$(row_counts "$RESTORE_URL")"
source="$(row_counts "$SOURCE_URL")"
echo "Rows in the restored copy:"
echo "$restored"
if [ "$restored" != "$source" ]; then
  echo "Test restore does not match the source database:" >&2
  diff <(echo "$source") <(echo "$restored") >&2 || true
  exit 1
fi

gpg --batch --yes --quiet --pinentry-mode loopback --passphrase-fd 3 \
    --symmetric --cipher-algo AES256 --output "$dump.gpg" "$dump" 3<<<"$BACKUP_PASSPHRASE"
echo "Encrypted backup: $dump.gpg"
