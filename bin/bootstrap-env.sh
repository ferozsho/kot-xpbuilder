#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="$root/.env"
instance=""
host_port="8088"
phpmyadmin_port=""
allowed_origins=""

usage() {
    echo "Usage: bootstrap-env.sh --instance NAME [options]" >&2
    echo "Options: --env-file /path/.env --host-port PORT --allowed-origins CSV" >&2
    echo "         --phpmyadmin-port PORT (default: host port + 1)" >&2
    exit 2
}

while [ "$#" -gt 0 ]; do
    case "$1" in
        --env-file) env_file="${2:-}"; shift 2 ;;
        --instance) instance="${2:-}"; shift 2 ;;
        --host-port) host_port="${2:-}"; shift 2 ;;
        --allowed-origins) allowed_origins="${2:-}"; shift 2 ;;
        --phpmyadmin-port) phpmyadmin_port="${2:-}"; shift 2 ;;
        *) usage ;;
    esac
done

if [ "$(basename "$env_file")" != ".env" ]; then
    echo "ERROR: the configuration file must be named exactly .env" >&2
    exit 1
fi

[ -n "$instance" ] || usage

if ! [[ "$instance" =~ ^[a-z][a-z0-9_-]*$ ]] \
    || ! [[ "$host_port" =~ ^[0-9]+$ ]]; then
    echo "ERROR: invalid instance or port" >&2
    exit 1
fi

# phpMyAdmin is published on the loopback interface only; keep its port next to
# the Superset one unless the site asks for a specific port.
if [ -z "$phpmyadmin_port" ]; then
    phpmyadmin_port=$((host_port + 1))
fi
if ! [[ "$phpmyadmin_port" =~ ^[0-9]+$ ]] \
    || [ "$phpmyadmin_port" -lt 1 ] || [ "$phpmyadmin_port" -gt 65535 ] \
    || [ "$phpmyadmin_port" -eq "$host_port" ]; then
    echo "ERROR: invalid phpMyAdmin port" >&2
    exit 1
fi

if [ -e "$env_file" ]; then
    echo "ERROR: refusing to overwrite existing $env_file" >&2
    exit 1
fi

command -v openssl >/dev/null 2>&1 || {
    echo "ERROR: openssl is required to generate deployment secrets" >&2
    exit 1
}

if [ -z "$allowed_origins" ]; then
    allowed_origins="http://localhost:${host_port},https://localhost:${host_port}"
fi

secret() {
    openssl rand -hex "$1"
}

umask 077
mkdir -p "$(dirname "$env_file")"

postgres_password="$(secret 24)"
redis_password="$(secret 24)"
superset_secret="$(secret 32)"
guest_secret="$(secret 32)"
admin_password="$(secret 18)"
mariadb_password="$(secret 18)"
mariadb_root_password="$(secret 24)"
version="$(cat "$root/VERSION")"

{
    printf 'XPBUILDER_VERSION=%s\n' "$version"
    printf 'XPBUILDER_IMAGE=kot-xpbuilder:local\n'
    printf 'XPBUILDER_INSTANCE=%s\n' "$instance"
    printf 'XPBUILDER_HOST_PORT=%s\n' "$host_port"
    printf 'XPBUILDER_INTERNAL_NETWORK=%s_xpbuilder_internal\n' "$instance"
    printf 'XPBUILDER_METADATA_VOLUME=%s_xpbuilder_metadata\n' "$instance"
    printf 'XPBUILDER_REDIS_VOLUME=%s_xpbuilder_redis\n' "$instance"
    printf 'XPBUILDER_MARIADB_VOLUME=%s_xpbuilder_mariadb\n' "$instance"
    printf 'XPBUILDER_VOLUMES_EXTERNAL=false\n'
    printf 'XPBUILDER_ALLOWED_ORIGINS=%s\n' "$allowed_origins"
    printf 'XPBUILDER_APP_NAME=Advance BI\n'
    # Optional URL the navbar brand logo links to; empty keeps "/".
    printf 'XPBUILDER_BRAND_URL=\n'
    printf 'XPBUILDER_ALLOW_INITIALIZE=yes\n'
    printf 'XPBUILDER_ALLOW_SCHEMA_UPGRADE=no\n'
    printf 'XPBUILDER_ALLOW_RESTORE=no\n'
    # Built-in database that uploaded CSV/Excel files are stored in (created
    # during `init`); set to `no` to leave the upload menu disabled.
    printf 'XPBUILDER_ENABLE_FILE_UPLOADS=yes\n'
    printf 'XPBUILDER_UPLOAD_DB_NAME=File uploads\n'
    # Bundled MariaDB: MARIADB_USER / MARIADB_PASSWORD are the credentials a
    # person types into phpMyAdmin, so they may be a site-chosen value.
    printf 'MARIADB_DATABASE=xpbuilder\n'
    printf 'MARIADB_USER=xpbuilder\n'
    printf 'MARIADB_PASSWORD=%s\n' "$mariadb_password"
    printf 'MARIADB_ROOT_PASSWORD=%s\n' "$mariadb_root_password"
    # phpMyAdmin is published on 127.0.0.1 only; the public entry point is the
    # TLS reverse proxy named by XPBUILDER_PHPMYADMIN_URL (empty = auto-detect).
    printf 'XPBUILDER_PHPMYADMIN_HOST_PORT=%s\n' "$phpmyadmin_port"
    printf 'XPBUILDER_PHPMYADMIN_URL=\n'
    printf 'POSTGRES_USER=xpbuilder\n'
    printf 'POSTGRES_PASSWORD=%s\n' "$postgres_password"
    printf 'POSTGRES_DB=xpbuilder\n'
    printf 'SUPERSET_REDIS_PASSWORD=%s\n' "$redis_password"
    printf 'SUPERSET_SECRET_KEY=%s\n' "$superset_secret"
    printf 'GUEST_TOKEN_JWT_SECRET=%s\n' "$guest_secret"
    printf 'GUEST_TOKEN_JWT_AUDIENCE=http://localhost:%s\n' "$host_port"
    printf 'SUPERSET_ADMIN_USERNAME=superset_admin\n'
    printf 'SUPERSET_ADMIN_PASSWORD=%s\n' "$admin_password"
    printf 'SUPERSET_ADMIN_FIRSTNAME=XPBuilder\n'
    printf 'SUPERSET_ADMIN_LASTNAME=Administrator\n'
    printf 'SUPERSET_ADMIN_EMAIL=xpbuilder-admin@mailinator.com\n'
    printf 'SUPERSET_LOG_LEVEL=INFO\n'
} > "$env_file"

unset postgres_password redis_password superset_secret guest_secret admin_password
unset mariadb_password mariadb_root_password

chmod 0600 "$env_file"
echo "Created protected deployment configuration: $env_file"
echo "Review it, then run: bin/xpbuilder --env-file $env_file init"
