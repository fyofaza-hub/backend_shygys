#!/usr/bin/env bash
# PostgreSQL custom-format archives are compressed and restored with pg_restore.
# Credentials: use PGHOST/PGPORT/PGDATABASE/PGUSER and a mode-0600 PGPASSFILE.
set -Eeuo pipefail
umask 077

: "${PGDATABASE:?Set PGDATABASE and PostgreSQL connection environment variables}"
backup_dir="${BACKUP_DIR:-/var/backups/creative-shygys}"
for tool in pg_dump pg_restore flock find; do
    command -v "$tool" >/dev/null || { echo "Required command missing: $tool" >&2; exit 1; }
done
mkdir -p -- "$backup_dir"
chmod 700 -- "$backup_dir"
exec 9>"$backup_dir/.backup.lock"
flock -n 9 || { echo "Another backup is running" >&2; exit 1; }

archive_tmp=$(mktemp "$backup_dir/.shygys-backup.XXXXXXXX.dump")
trap 'rm -f -- "$archive_tmp"' EXIT
archive_final="$backup_dir/shygys_$(date -u +%Y%m%dT%H%M%S)_${archive_tmp##*.shygys-backup.}"
pg_dump --no-password --format=custom --compress=6 --file="$archive_tmp"
pg_restore --list "$archive_tmp" >/dev/null
mv -- "$archive_tmp" "$archive_final"
# Match only this script's completed archives; retain the last 14 * 24 hours.
find "$backup_dir" -maxdepth 1 -type f -name 'shygys_*.dump' -mmin +20160 -delete
printf 'Backup created: %s\n' "$archive_final"
