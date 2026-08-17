#!/bin/bash
set -euo pipefail

if [[ -z "${GPA_BACKUP_DATABASE_URL:-}" ]]; then
  echo "Set GPA_BACKUP_DATABASE_URL to the PostgreSQL backup connection." >&2
  exit 1
fi

repo_dir="$(cd "$(dirname "$0")/.." && pwd)"
backup_dir="${GPA_BACKUP_DIRECTORY:-$repo_dir/artifacts/backups}"
mkdir -p "$backup_dir"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
output="$backup_dir/gpa-$timestamp.dump"

pg_dump --format=custom --no-owner --no-acl --file="$output" "$GPA_BACKUP_DATABASE_URL"
shasum -a 256 "$output" > "$output.sha256"
chmod 600 "$output" "$output.sha256"
echo "Backup written: $output"
