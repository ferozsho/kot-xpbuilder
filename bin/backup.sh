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
timestamp="$(date -u +%Y%m%d-%H%M%S)"
destination="$backup_root/$project/$timestamp"
backup="$destination/superset-metadata.dump"
partial="$backup.partial"

umask 077
mkdir -p "$destination"
trap 'rm -f "$partial"' EXIT

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
image_id="$(docker inspect --format '{{.Image}}' "${project}_superset" 2>/dev/null || true)"

python3 - "$destination/manifest.json" "$project" "$timestamp" "$checksum" "$image_id" <<'PY'
import json
import sys

manifest = {
    'schema_version': 1,
    'instance': sys.argv[2],
    'created_at_utc': sys.argv[3],
    'metadata_backup': 'superset-metadata.dump',
    'sha256': sys.argv[4],
    'superset_image_id': sys.argv[5] or None,
}
with open(sys.argv[1], 'w', encoding='utf-8') as handle:
    json.dump(manifest, handle, indent=2)
    handle.write('\n')
PY

chmod 0600 "$backup" "$destination/manifest.json"
echo "Backup completed: $destination"
