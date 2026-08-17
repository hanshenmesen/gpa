#!/bin/bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/gpa-backup.dump" >&2
  exit 1
fi
if [[ -z "${GPA_RESTORE_ADMIN_URL:-}" ]]; then
  echo "Set GPA_RESTORE_ADMIN_URL to a PostgreSQL admin connection." >&2
  exit 1
fi

backup_file="$1"
if [[ ! -f "$backup_file" ]]; then
  echo "Backup not found: $backup_file" >&2
  exit 1
fi

restore_db="gpa_restore_check_$(date -u +%Y%m%d%H%M%S)_$$"
cleanup() {
  dropdb --if-exists --force --maintenance-db="$GPA_RESTORE_ADMIN_URL" "$restore_db" >/dev/null
}
trap cleanup EXIT

createdb --maintenance-db="$GPA_RESTORE_ADMIN_URL" "$restore_db"
pg_restore --exit-on-error --no-owner --no-acl --dbname="$restore_db" "$backup_file"
psql --dbname="$restore_db" --no-psqlrc --tuples-only --command="SELECT count(*) FROM schema_migrations"
echo "Restore verification passed: $backup_file"
