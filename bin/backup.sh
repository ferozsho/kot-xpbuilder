#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:?env file required}"
project="${2:?compose project required}"
backup_root="${3:-$root/backups}"

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

postgres_user="$(value_of POSTGRES_USER)"
postgres_db="$(value_of POSTGRES_DB)"
# Uploaded CSV/Excel/columnar files are stored in a second database inside the
# same PostgreSQL instance (see docker/ensure_uploads_db.py). It is dumped too,
# so a restore can never silently lose uploaded data.
uploads_db="xpbuilder_uploads"
timestamp="$(date -u +%Y%m%d-%H%M%S)"
destination="$backup_root/$project/$timestamp"
backup="$destination/superset-metadata.dump"
partial="$backup.partial"
uploads_backup="$destination/uploads.dump"
uploads_partial="$uploads_backup.partial"

umask 077
mkdir -p "$destination"
trap 'rm -f "$partial" "$uploads_partial"' EXIT

compose=(docker compose --env-file "$env_file" -f "$root/compose.yml" -p "$project")

echo "Creating XPBuilder metadata backup for $project"
"${compose[@]}" exec -T superset-db \
    pg_dump -U "$postgres_user" -d "$postgres_db" --format=custom > "$partial"

if [ ! -s "$partial" ]; then
    echo "ERROR: PostgreSQL backup is empty" >&2
    exit 1
fi

mv "$partial" "$backup"
checksum="$(sha256sum "$backup" | awk '{print $1}')"

uploads_present="$("${compose[@]}" exec -T superset-db \
    psql -U "$postgres_user" -d postgres -tAc \
    "SELECT 1 FROM pg_database WHERE datname = '${uploads_db}'")"
uploads_checksum=""
if [ "$uploads_present" = "1" ]; then
    echo "Backing up the file upload store ($uploads_db)"
    "${compose[@]}" exec -T superset-db \
        pg_dump -U "$postgres_user" -d "$uploads_db" --format=custom \
        > "$uploads_partial"
    if [ ! -s "$uploads_partial" ]; then
        echo "ERROR: $uploads_db backup is empty" >&2
        exit 1
    fi
    mv "$uploads_partial" "$uploads_backup"
    uploads_checksum="$(sha256sum "$uploads_backup" | awk '{print $1}')"
    chmod 0600 "$uploads_backup"
fi

image_id="$(docker inspect --format '{{.Image}}' "${project}_superset" 2>/dev/null || true)"

python3 - "$destination/manifest.json" "$project" "$timestamp" "$checksum" \
    "$image_id" "$uploads_db" "$uploads_checksum" <<'PY'
import json
import sys

uploads_checksum = sys.argv[7]
manifest = {
    'schema_version': 2,
    'instance': sys.argv[2],
    'created_at_utc': sys.argv[3],
    'metadata_backup': 'superset-metadata.dump',
    'sha256': sys.argv[4],
    'superset_image_id': sys.argv[5] or None,
    'uploads_database': sys.argv[6] if uploads_checksum else None,
    'uploads_backup': 'uploads.dump' if uploads_checksum else None,
    'uploads_sha256': uploads_checksum or None,
}
with open(sys.argv[1], 'w', encoding='utf-8') as handle:
    json.dump(manifest, handle, indent=2)
    handle.write('\n')
PY

chmod 0600 "$backup" "$destination/manifest.json"
echo "Backup completed: $destination"
