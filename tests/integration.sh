#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

# Never inherit a site configuration. Exported XPBUILDER_*/POSTGRES_*/SUPERSET_*
# values take precedence over --env-file, which would point this test at a real
# stack and let it create, converge, or delete that stack's containers and
# volumes.
while IFS= read -r variable; do
    case "$variable" in
        XPBUILDER_*|POSTGRES_*|SUPERSET_*|GUEST_TOKEN_*) unset "$variable" ;;
    esac
done < <(compgen -v)

suffix="$(date +%s)-$$"
instance="xpbuildertest${suffix//-/}"
temporary="$(mktemp -d)"
env_file="$temporary/.env"

port="$(python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(('127.0.0.1', 0))
    print(sock.getsockname()[1])
PY
)"

cleanup() {
    set +e
    docker compose --env-file "$env_file" -f compose.yml -p "$instance" \
        down --volumes --remove-orphans >/dev/null 2>&1
    rm -rf "$temporary"
}
trap cleanup EXIT

bin/bootstrap-env.sh \
    --env-file "$env_file" \
    --instance "$instance" \
    --host-port "$port" >/dev/null

compose=(docker compose --env-file "$env_file" -f compose.yml -p "$instance")

# Refuse to run if anything still resolves the stack somewhere else: this test
# creates and deletes containers and volumes.
resolved_container="$("${compose[@]}" config --format json \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"]["superset"]["container_name"])')"
if [ "$resolved_container" != "${instance}_superset" ]; then
    echo "ERROR: refusing to run: compose resolved container '$resolved_container'" >&2
    echo "       expected '${instance}_superset' (check for exported site variables)" >&2
    exit 1
fi

"${compose[@]}" build
"${compose[@]}" up -d superset-db superset-redis
"${compose[@]}" --profile tools run --rm initialize \
    /opt/xpbuilder/bin/initialize.sh new
# Bring up the whole stack, including the bundled MariaDB and phpMyAdmin.
"${compose[@]}" up -d

for attempt in $(seq 1 60); do
    if curl --fail --silent --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        "${compose[@]}" ps -a
        "${compose[@]}" logs --tail=200 superset superset-worker superset-beat
        echo "ERROR: XPBuilder web service did not become ready" >&2
        exit 1
    fi
    sleep 3
done

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

python3 tests/contract/runtime_contract.py \
    --base-url "http://127.0.0.1:${port}" \
    --username "$(value_of SUPERSET_ADMIN_USERNAME)" \
    --password "$(value_of SUPERSET_ADMIN_PASSWORD)"

services=(
    superset superset-worker superset-beat superset-db superset-redis
    mariadb phpmyadmin
)
for attempt in $(seq 1 40); do
    all_healthy=1
    for service in "${services[@]}"; do
        container_id="$("${compose[@]}" ps -q "$service")"
        if [ -z "$container_id" ]; then
            all_healthy=0
            continue
        fi
        state="$(docker inspect --format '{{.State.Status}}' "$container_id")"
        health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id")"
        if [ "$state" != "running" ] || { [ "$health" != "healthy" ] && [ "$health" != "none" ]; }; then
            all_healthy=0
        fi
    done
    if [ "$all_healthy" -eq 1 ]; then
        break
    fi
    if [ "$attempt" -eq 40 ]; then
        "${compose[@]}" ps -a
        "${compose[@]}" logs --tail=200 "${services[@]}"
        echo "ERROR: one or more XPBuilder services did not become healthy" >&2
        exit 1
    fi
    sleep 3
done

uploads_psql() {
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d xpbuilder_uploads -tAc "$1"
}

metadata_psql() {
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d "$(value_of POSTGRES_DB)" -tAc "$1"
}

api() {
    curl --silent --fail --max-time 30 "$@"
}

echo "Checking that file uploads are provisioned"
token="$(api -X POST "http://127.0.0.1:${port}/api/v1/security/login" \
    -H 'Content-Type: application/json' \
    -d "{\"username\":\"$(value_of SUPERSET_ADMIN_USERNAME)\",\"password\":\"$(value_of SUPERSET_ADMIN_PASSWORD)\",\"provider\":\"db\",\"refresh\":true}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["access_token"])')"

upload_query='(filters:!((col:allow_file_upload,opr:upload_is_enabled,value:!t)))'
upload_view="$(api "http://127.0.0.1:${port}/api/v1/database/?q=${upload_query}" \
    -H "Authorization: Bearer ${token}")"
upload_capable="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["count"])' <<< "$upload_view")"
if [ "$upload_capable" != "1" ]; then
    echo "ERROR: expected exactly one upload-capable connection, got '$upload_capable'" >&2
    exit 1
fi
database_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["result"][0]["id"])' <<< "$upload_view")"

echo "Uploading a CSV into the built-in upload store"
printf 'city,sales\nSkopje,4200\nPristina,3100\n' > "$temporary/upload-check.csv"
api -X POST "http://127.0.0.1:${port}/api/v1/database/${database_id}/upload/" \
    -H "Authorization: Bearer ${token}" \
    -F type=csv -F table_name=integration_upload -F schema=public \
    -F "file=@$temporary/upload-check.csv" >/dev/null

uploaded_rows="$(uploads_psql 'SELECT count(*) FROM integration_upload')"
if [ "$uploaded_rows" != "2" ]; then
    echo "ERROR: uploaded CSV produced '$uploaded_rows' rows, expected 2" >&2
    exit 1
fi

echo "Checking that duplicate uploads replace instead of duplicating records"
api -X POST "http://127.0.0.1:${port}/api/v1/database/${database_id}/upload/" \
    -H "Authorization: Bearer ${token}" \
    -F type=csv -F table_name=integration_upload -F schema=public \
    -F already_exists=replace \
    -F "file=@$temporary/upload-check.csv" >/dev/null

replaced_rows="$(uploads_psql 'SELECT count(*) FROM integration_upload')"
if [ "$replaced_rows" != "2" ]; then
    echo "ERROR: replacement upload produced '$replaced_rows' rows, expected 2" >&2
    exit 1
fi
upload_datasets="$(metadata_psql "SELECT count(*) FROM tables WHERE table_name = 'integration_upload'")"
if [ "$upload_datasets" != "1" ]; then
    echo "ERROR: replacement upload produced '$upload_datasets' datasets, expected 1" >&2
    exit 1
fi

echo "Checking that a header-only CSV is rejected without creating a table"
printf 'city,sales\n' > "$temporary/header-only.csv"
empty_response="$temporary/header-only-response.json"
empty_status="$(curl --silent --max-time 30 \
    --output "$empty_response" --write-out '%{http_code}' \
    -X POST "http://127.0.0.1:${port}/api/v1/database/${database_id}/upload/" \
    -H 'Accept: application/json' \
    -H "Authorization: Bearer ${token}" \
    -F type=csv -F table_name=integration_empty_upload -F schema=public \
    -F "file=@$temporary/header-only.csv")"
if [ "$empty_status" != "422" ]; then
    echo "ERROR: header-only upload returned HTTP '$empty_status', expected 422" >&2
    cat "$empty_response" >&2
    exit 1
fi
if ! grep -q 'contains headers but no data rows' "$empty_response"; then
    echo "ERROR: header-only upload did not return an actionable message" >&2
    cat "$empty_response" >&2
    exit 1
fi
empty_table="$(uploads_psql "SELECT to_regclass('public.integration_empty_upload') IS NULL")"
if [ "$empty_table" != "t" ]; then
    echo "ERROR: header-only upload created an empty database table" >&2
    exit 1
fi

mariadb_query() {
    "${compose[@]}" exec -T mariadb sh -c \
        "exec env MYSQL_PWD=\"\$MARIADB_PASSWORD\" mariadb -u \"\$MARIADB_USER\" \"\$MARIADB_DATABASE\" -e \"\$1\"" \
        sh "$1"
}

mariadb_rows() {
    "${compose[@]}" exec -T mariadb sh -c \
        "exec env MYSQL_PWD=\"\$MARIADB_PASSWORD\" mariadb -u \"\$MARIADB_USER\" \"\$MARIADB_DATABASE\" -N -B -e \"\$1\"" \
        sh "$1"
}

echo "Checking that the bundled MariaDB accepts the site credentials"
mariadb_query 'CREATE TABLE integration_marker (id INT PRIMARY KEY, note VARCHAR(32));
INSERT INTO integration_marker VALUES (1, "backup-marker")'
marker_rows="$(mariadb_rows 'SELECT count(*) FROM integration_marker')"
if [ "$marker_rows" != "1" ]; then
    echo "ERROR: bundled MariaDB returned '$marker_rows' marker rows, expected 1" >&2
    exit 1
fi

echo "Backing up metadata plus uploaded data"
backup_root="$temporary/backups"
backup_dir="$(bin/backup.sh "$env_file" "$instance" "$backup_root" \
    | sed -n 's/^Backup completed: //p')"
if [ ! -s "$backup_dir/uploads.dump" ]; then
    echo "ERROR: backup did not include the upload store ($backup_dir)" >&2
    ls -l "$backup_dir"
    exit 1
fi
if [ ! -s "$backup_dir/mariadb.dump" ]; then
    echo "ERROR: backup did not include the bundled MariaDB ($backup_dir)" >&2
    ls -l "$backup_dir"
    exit 1
fi
if ! grep -q integration_marker "$backup_dir/mariadb.dump"; then
    echo "ERROR: MariaDB dump does not contain the marker table" >&2
    exit 1
fi

echo "Simulating loss of the upload store and the MariaDB table, then restoring"
"${compose[@]}" exec -T superset-db \
    psql -U "$(value_of POSTGRES_USER)" -d postgres \
    -c 'DROP DATABASE xpbuilder_uploads WITH (FORCE)' >/dev/null
mariadb_query 'DROP TABLE integration_marker'

sed -i 's/^XPBUILDER_ALLOW_RESTORE=no$/XPBUILDER_ALLOW_RESTORE=yes/' "$env_file"
bin/restore.sh "$env_file" "$instance" \
    "$backup_dir/superset-metadata.dump" "restore-$instance" >/dev/null

for attempt in $(seq 1 60); do
    if curl --fail --silent --max-time 5 "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
        break
    fi
    if [ "$attempt" -eq 60 ]; then
        echo "ERROR: XPBuilder web service did not come back after the restore" >&2
        exit 1
    fi
    sleep 3
done

restored_rows="$(uploads_psql 'SELECT count(*) FROM integration_upload')"
if [ "$restored_rows" != "2" ]; then
    echo "ERROR: restore produced '$restored_rows' uploaded rows, expected 2" >&2
    exit 1
fi
restored_connections="$(metadata_psql 'SELECT count(*) FROM dbs WHERE allow_file_upload')"
if [ "$restored_connections" != "1" ]; then
    echo "ERROR: restore left '$restored_connections' upload-capable connections" >&2
    exit 1
fi

restored_marker="$(mariadb_rows 'SELECT count(*) FROM integration_marker')"
if [ "$restored_marker" != "1" ]; then
    echo "ERROR: restore produced '$restored_marker' MariaDB marker rows, expected 1" >&2
    exit 1
fi

echo "XPBuilder integration checks passed"
