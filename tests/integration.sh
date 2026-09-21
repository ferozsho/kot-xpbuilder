#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

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

"${compose[@]}" build
"${compose[@]}" up -d superset-db superset-redis
"${compose[@]}" --profile tools run --rm initialize \
    /opt/xpbuilder/bin/initialize.sh new
"${compose[@]}" up -d superset superset-worker superset-beat

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

services=(superset superset-worker superset-beat superset-db superset-redis)
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

echo "XPBuilder integration checks passed"
