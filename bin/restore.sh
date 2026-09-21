#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:?env file required}"
project="${2:?compose project required}"
backup="${3:?backup file required}"
confirmation="${4:-}"

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

if [ "$(value_of XPBUILDER_ALLOW_RESTORE)" != "yes" ]; then
    echo "ERROR: restore requires XPBUILDER_ALLOW_RESTORE=yes in .env" >&2
    exit 1
fi
if [ "$confirmation" != "restore-$project" ]; then
    echo "ERROR: confirmation must be exactly restore-$project" >&2
    exit 1
fi
if [ ! -s "$backup" ]; then
    echo "ERROR: backup file is missing or empty: $backup" >&2
    exit 1
fi

postgres_user="$(value_of POSTGRES_USER)"
postgres_db="$(value_of POSTGRES_DB)"
compose=(docker compose --env-file "$env_file" -f "$root/compose.yml" -p "$project")

echo "Stopping XPBuilder application services before metadata restore"
"${compose[@]}" stop superset superset-worker superset-beat

echo "Restoring PostgreSQL metadata for $project"
"${compose[@]}" exec -T superset-db \
    pg_restore -U "$postgres_user" -d "$postgres_db" \
    --clean --if-exists --no-owner < "$backup"

echo "Restarting XPBuilder application services"
"${compose[@]}" up -d superset superset-worker superset-beat
echo "Restore completed; run bin/xpbuilder health before re-enabling Advanced BI"
