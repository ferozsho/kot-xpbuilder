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
# The bundled MariaDB (site data managed in phpMyAdmin) is dumped too, so a
# restore can never silently lose it.
mariadb_backup="$destination/mariadb.dump"
mariadb_partial="$mariadb_backup.partial"

umask 077
mkdir -p "$destination"
trap 'rm -f "$partial" "$uploads_partial" "$mariadb_partial" "$ai_partial"' EXIT

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

ai_db="xprompt"
ai_backup="$destination/ai.dump"
ai_partial="$ai_backup.partial"
ai_checksum=""
ai_present="$("${compose[@]}" ps -q xp-prompt-db 2>/dev/null || true)"
if [ -n "$ai_present" ] \
    && [ "$(docker inspect --format '{{.State.Running}}' "$ai_present")" = "true" ]; then
    # The KOT5 AI database is a durable store in its own right: conversations,
    # jobs, the usage ledger, repair patterns, drafts, settings and their audit.
    # Leaving it out of the backup would restore a deployment that has forgotten
    # everything it did (P11-F08).
    echo "Backing up the AI service database ($ai_db)"
    "${compose[@]}" exec -T xp-prompt-db \
        pg_dump -U xprompt -d "$ai_db" --format=custom > "$ai_partial"
    if [ ! -s "$ai_partial" ]; then
        echo "ERROR: $ai_db backup is empty" >&2
        exit 1
    fi
    mv "$ai_partial" "$ai_backup"
    ai_checksum="$(sha256sum "$ai_backup" | awk '{print $1}')"
    chmod 0600 "$ai_backup"
fi

# Vector store: snapshots are the supported export, so they are taken per
# collection rather than by copying files out of the volume.
qdrant_dir="$destination/qdrant"
qdrant_snapshots=""
qdrant_id="$("${compose[@]}" ps -q xp-prompt-qdrant 2>/dev/null || true)"
api_id="$("${compose[@]}" ps -q xp-prompt-api 2>/dev/null || true)"
if [ -n "$qdrant_id" ] && [ -n "$api_id" ] \
    && [ "$(docker inspect --format '{{.State.Running}}' "$qdrant_id")" = "true" ] \
    && [ "$(docker inspect --format '{{.State.Running}}' "$api_id")" = "true" ]; then
    # The service already knows where Qdrant is, so the helper runs there rather
    # than needing a second copy of the URL and a client in this shell.
    mkdir -p "$qdrant_dir"
    for collection in $("${compose[@]}" exec -T xp-prompt-api \
        /app/.venv/bin/python /app/xp_prompt/tools/qdrant_snapshot.py list \
        2>/dev/null); do
        [ -z "$collection" ] && continue
        echo "Snapshotting Qdrant collection $collection"
        "${compose[@]}" exec -T xp-prompt-api \
            /app/.venv/bin/python /app/xp_prompt/tools/qdrant_snapshot.py \
            download "$collection" > "$qdrant_dir/$collection.snapshot" 2>/dev/null || true
        if [ -s "$qdrant_dir/$collection.snapshot" ]; then
            qdrant_snapshots="$qdrant_snapshots$collection.snapshot "
            chmod 0600 "$qdrant_dir/$collection.snapshot"
        else
            rm -f "$qdrant_dir/$collection.snapshot"
        fi
    done
fi

image_id="$(docker inspect --format '{{.Image}}' "${project}_superset" 2>/dev/null || true)"

# The bundled MariaDB is backed up with its own dump. A stack that has not been
# converged since the database was added simply has no container to dump, so
# the section is skipped rather than failing the backup.
mariadb_db="$(value_of MARIADB_DATABASE)"
# Read the credential from .env rather than from the container environment: the
# environment is only applied when the image creates an empty volume, so a
# rotated password would otherwise keep using the old value here.
mariadb_root_password="$(value_of MARIADB_ROOT_PASSWORD)"
mariadb_checksum=""
mariadb_id="$("${compose[@]}" ps -q mariadb 2>/dev/null || true)"
if [ -n "$mariadb_id" ] \
    && [ "$(docker inspect --format '{{.State.Running}}' "$mariadb_id")" = "true" ]; then
    echo "Backing up the MariaDB data store ($mariadb_db)"
    "${compose[@]}" exec -T -e MYSQL_PWD="$mariadb_root_password" mariadb \
        mariadb-dump -u root --single-transaction --routines --triggers \
        --events --databases "$mariadb_db" > "$mariadb_partial"
    if [ ! -s "$mariadb_partial" ]; then
        echo "ERROR: MariaDB backup is empty" >&2
        exit 1
    fi
    mv "$mariadb_partial" "$mariadb_backup"
    mariadb_checksum="$(sha256sum "$mariadb_backup" | awk '{print $1}')"
    chmod 0600 "$mariadb_backup"
fi

# Row counts are the read-back half of a backup: a dump that restores is not the
# same as a dump that restored the right number of records.
# Real counts, not planner estimates: the point of the drill is to prove the
# restored rows, and n_live_tup is only an estimate.
ai_counts_file="$destination/ai-row-counts.txt"
: > "$ai_counts_file"
if [ -n "$ai_checksum" ]; then
    # Exact counts: pg_stat is an estimate, and an estimate cannot prove a restore.
    "${compose[@]}" exec -T xp-prompt-db \
        psql -U xprompt -d "$ai_db" -tAF'|' -c \
        "SELECT relname, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname), false, true, '')))[1]::text::int FROM pg_stat_user_tables ORDER BY relname" \
        > "$ai_counts_file"
fi

python3 - "$destination/manifest.json" "$project" "$timestamp" "$checksum" \
    "$image_id" "$uploads_db" "$uploads_checksum" "$mariadb_db" \
    "$mariadb_checksum" "$ai_db" "$ai_checksum" "${qdrant_snapshots:-}" \
    "$ai_counts_file" <<'PY'
import json
import sys

uploads_checksum = sys.argv[7]
mariadb_checksum = sys.argv[9]
manifest = {
    'schema_version': 4,
    'instance': sys.argv[2],
    'created_at_utc': sys.argv[3],
    'metadata_backup': 'superset-metadata.dump',
    'sha256': sys.argv[4],
    'superset_image_id': sys.argv[5] or None,
    'uploads_database': sys.argv[6] if uploads_checksum else None,
    'uploads_backup': 'uploads.dump' if uploads_checksum else None,
    'uploads_sha256': uploads_checksum or None,
    'mariadb_database': sys.argv[8] if mariadb_checksum else None,
    'mariadb_backup': 'mariadb.dump' if mariadb_checksum else None,
    'mariadb_sha256': mariadb_checksum or None,
    'ai_database': sys.argv[10] if sys.argv[11] else None,
    'ai_backup': 'ai.dump' if sys.argv[11] else None,
    'ai_sha256': sys.argv[11] or None,
    'qdrant_snapshots': sorted(sys.argv[12].split()) or None,
    'ai_row_counts': {
        line.split('|')[0]: int(line.split('|')[1] or 0)
        for line in open(sys.argv[13], encoding='utf-8').read().splitlines()
        if '|' in line
    },
    # The AI provider keys are sealed with the deployment's root secret, which is
    # deliberately not in this backup: a restore needs the same
    # XP_PROMPT_SERVICE_SECRET from the environment, or those rows are unreadable.
    'secrets_not_included': ['XP_PROMPT_SERVICE_SECRET', 'SECRET_KEY',
                             'XP_PROMPT_POSTGRES_PASSWORD', 'POSTGRES_PASSWORD',
                             'XP_PROMPT_REDIS_PASSWORD', 'MARIADB_ROOT_PASSWORD'],
}
with open(sys.argv[1], 'w', encoding='utf-8') as handle:
    json.dump(manifest, handle, indent=2)
    handle.write('\n')
PY

chmod 0600 "$backup" "$destination/manifest.json"
echo "Backup completed: $destination"
