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
# Uploaded CSV/Excel/columnar files live in their own database inside the same
# PostgreSQL instance (see docker/ensure_uploads_db.py) and are restored too.
uploads_db="xpbuilder_uploads"
compose=(docker compose --env-file "$env_file" -f "$root/compose.yml" -p "$project")

# Locate the upload store dump: prefer the manifest, and fall back to any
# sibling *uploads*.dump so backups written by the client ops wrapper restore
# with the same command.
backup_dir="$(cd "$(dirname "$backup")" && pwd)"
uploads_dump=""
if [ -f "$backup_dir/manifest.json" ]; then
    uploads_file="$(python3 - "$backup_dir/manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as handle:
    manifest = json.load(handle)
print(manifest.get('uploads_backup') or '')
PY
)"
    if [ -n "$uploads_file" ]; then
        uploads_dump="$backup_dir/$uploads_file"
    fi
fi
if [ ! -s "$uploads_dump" ]; then
    uploads_dump=""
    for candidate in "$backup_dir"/*uploads*.dump; do
        if [ -s "$candidate" ]; then
            uploads_dump="$candidate"
            break
        fi
    done
fi

echo "Stopping XPBuilder application services before metadata restore"
"${compose[@]}" stop superset superset-worker superset-beat

echo "Restoring PostgreSQL metadata for $project"
"${compose[@]}" exec -T superset-db \
    pg_restore -U "$postgres_user" -d "$postgres_db" \
    --clean --if-exists --no-owner < "$backup"

if [ -n "$uploads_dump" ]; then
    echo "Restoring the file upload store from $(basename "$uploads_dump")"
    # The upload role, database, and connection are provisioned by the image.
    # Re-run the provisioner first so restoring onto a fresh volume recreates
    # them (and converges the connection) before the data is loaded.
    "${compose[@]}" run --rm --no-deps -T \
        --entrypoint /app/.venv/bin/python superset \
        /opt/xpbuilder/bin/ensure_uploads_db.py
    "${compose[@]}" exec -T superset-db \
        pg_restore -U "$postgres_user" -d "$uploads_db" \
        --clean --if-exists < "$uploads_dump"
fi

# The bundled MariaDB (site data managed in phpMyAdmin) travels with the backup
# set as mariadb.dump. Locate it through the manifest, and fall back to any
# sibling *mariadb*.dump so dumps written by the client ops wrapper restore
# with the same command.
mariadb_dump=""
if [ -f "$backup_dir/manifest.json" ]; then
    mariadb_file="$(python3 - "$backup_dir/manifest.json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding='utf-8') as handle:
    manifest = json.load(handle)
print(manifest.get('mariadb_backup') or '')
PY
)"
    if [ -n "$mariadb_file" ]; then
        mariadb_dump="$backup_dir/$mariadb_file"
    fi
fi
if [ ! -s "$mariadb_dump" ]; then
    mariadb_dump=""
    for candidate in "$backup_dir"/*mariadb*.dump; do
        if [ -s "$candidate" ]; then
            mariadb_dump="$candidate"
            break
        fi
    done
fi

if [ -n "$mariadb_dump" ]; then
    echo "Restoring the MariaDB data store from $(basename "$mariadb_dump")"
    # Bring the server up first (a restore often runs on a stopped stack) and
    # wait for it to accept connections before loading the dump.
    "${compose[@]}" up -d --wait mariadb
    "${compose[@]}" exec -T mariadb sh -c \
        "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root" \
        < "$mariadb_dump"
fi

echo "Restarting XPBuilder application services"
"${compose[@]}" up -d superset superset-worker superset-beat
echo "Restore completed; run bin/xpbuilder health before re-enabling Advanced BI"