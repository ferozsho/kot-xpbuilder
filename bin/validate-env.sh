#!/usr/bin/env bash

set -euo pipefail

allow_group_env="no"
env_file=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --allow-group-env) allow_group_env="yes"; shift ;;
        *) env_file="$1"; shift ;;
    esac
done

if [ -z "$env_file" ] || [ ! -f "$env_file" ]; then
    echo "ERROR: .env file not found: ${env_file:-<not provided>}" >&2
    exit 1
fi

if [ "$(basename "$env_file")" != ".env" ]; then
    echo "ERROR: configuration file must be named exactly .env" >&2
    exit 1
fi

mode="$(stat -c '%a' "$env_file")"
other_bits=$((10#$mode % 10))
group_bits=$(( (10#$mode / 10) % 10 ))
if [ "$other_bits" -ne 0 ]; then
    echo "ERROR: $env_file must not be readable or writable by other users (use chmod 600)" >&2
    exit 1
fi
if [ "$group_bits" -ne 0 ]; then
    if [ "$allow_group_env" != "yes" ]; then
        echo "ERROR: $env_file (mode $mode) is accessible to its group; secrets must stay private" >&2
        echo "       (use chmod 600), or pass --allow-group-env when a site deliberately" >&2
        echo "       shares .env with a restricted group, for example a jailed client." >&2
        exit 1
    fi
    echo "WARNING: $env_file is group-accessible (mode $mode); allowed by --allow-group-env" >&2
fi

duplicates="$(sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p' "$env_file" | sort | uniq -d)"
if [ -n "$duplicates" ]; then
    echo "ERROR: duplicate keys in $env_file" >&2
    printf '%s\n' "$duplicates" >&2
    exit 1
fi

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

required_keys=(
    XPBUILDER_INSTANCE XPBUILDER_HOST_PORT XPBUILDER_INTERNAL_NETWORK
    XPBUILDER_METADATA_VOLUME XPBUILDER_REDIS_VOLUME
    XPBUILDER_VOLUMES_EXTERNAL XPBUILDER_ALLOWED_ORIGINS
    POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB SUPERSET_REDIS_PASSWORD
    SUPERSET_SECRET_KEY GUEST_TOKEN_JWT_SECRET GUEST_TOKEN_JWT_AUDIENCE
    SUPERSET_ADMIN_USERNAME SUPERSET_ADMIN_PASSWORD SUPERSET_ADMIN_FIRSTNAME
    SUPERSET_ADMIN_LASTNAME SUPERSET_ADMIN_EMAIL
    # Bundled MariaDB + phpMyAdmin (see docs/configuration.md)
    XPBUILDER_MARIADB_VOLUME XPBUILDER_PHPMYADMIN_HOST_PORT
    MARIADB_DATABASE MARIADB_USER MARIADB_PASSWORD MARIADB_ROOT_PASSWORD
)

missing=()
for key in "${required_keys[@]}"; do
    [ -n "$(value_of "$key")" ] || missing+=("$key")
done
if [ "${#missing[@]}" -gt 0 ]; then
    echo "ERROR: missing required .env keys: ${missing[*]}" >&2
    exit 1
fi

instance="$(value_of XPBUILDER_INSTANCE)"
host_port="$(value_of XPBUILDER_HOST_PORT)"
external="$(value_of XPBUILDER_VOLUMES_EXTERNAL)"
origins="$(value_of XPBUILDER_ALLOWED_ORIGINS)"

[[ "$instance" =~ ^[a-z][a-z0-9_-]*$ ]] || {
    echo "ERROR: XPBUILDER_INSTANCE must match ^[a-z][a-z0-9_-]*$" >&2
    exit 1
}
[[ "$host_port" =~ ^[0-9]+$ ]] || {
    echo "ERROR: XPBUILDER_HOST_PORT must be numeric" >&2
    exit 1
}
if [ "$host_port" -lt 1 ] || [ "$host_port" -gt 65535 ]; then
    echo "ERROR: configured port is outside 1-65535" >&2
    exit 1
fi
[[ "$external" = "true" || "$external" = "false" ]] || {
    echo "ERROR: XPBUILDER_VOLUMES_EXTERNAL must be true or false" >&2
    exit 1
}
if [[ "$origins" == *'*'* ]]; then
    echo "ERROR: XPBUILDER_ALLOWED_ORIGINS must list explicit origins; wildcard is forbidden" >&2
    exit 1
fi

# MariaDB + phpMyAdmin: the database and user names end up in SQL identifiers,
# and the phpMyAdmin port must be dedicated to this stack.
mariadb_database="$(value_of MARIADB_DATABASE)"
mariadb_user="$(value_of MARIADB_USER)"
pma_host_port="$(value_of XPBUILDER_PHPMYADMIN_HOST_PORT)"
pma_url="$(value_of XPBUILDER_PHPMYADMIN_URL)"

[[ "$mariadb_database" =~ ^[A-Za-z0-9_]+$ ]] || {
    echo "ERROR: MARIADB_DATABASE must contain only letters, digits, and underscores" >&2
    exit 1
}
[[ "$mariadb_user" =~ ^[A-Za-z0-9_.-]+$ ]] || {
    echo "ERROR: MARIADB_USER must contain only letters, digits, dot, dash, and underscore" >&2
    exit 1
}
[[ "$pma_host_port" =~ ^[0-9]+$ ]] || {
    echo "ERROR: XPBUILDER_PHPMYADMIN_HOST_PORT must be numeric" >&2
    exit 1
}
if [ "$pma_host_port" -lt 1 ] || [ "$pma_host_port" -gt 65535 ]; then
    echo "ERROR: XPBUILDER_PHPMYADMIN_HOST_PORT is outside 1-65535" >&2
    exit 1
fi
if [ "$pma_host_port" = "$host_port" ]; then
    echo "ERROR: XPBUILDER_PHPMYADMIN_HOST_PORT must differ from XPBUILDER_HOST_PORT" >&2
    exit 1
fi
# phpMyAdmin's PMA_ABSOLUTE_URI needs an absolute, trailing-slash URL; leaving
# it empty lets phpMyAdmin auto-detect the address.
if [ -n "$pma_url" ]; then
    if [[ ! "$pma_url" =~ ^https?://[^[:space:]]*/$ ]]; then
        echo "ERROR: XPBUILDER_PHPMYADMIN_URL must be an http(s) URL ending in /" >&2
        exit 1
    fi
fi

secret_keys=(
    POSTGRES_PASSWORD SUPERSET_REDIS_PASSWORD SUPERSET_SECRET_KEY
    GUEST_TOKEN_JWT_SECRET MARIADB_ROOT_PASSWORD
)
# Human-facing credentials are typed by people (and are often fixed by a site
# requirement), so they only have to clear a basic 8-character floor.
human_secret_keys=(
    SUPERSET_ADMIN_PASSWORD MARIADB_PASSWORD
)
# Greenfield floor is 16 characters for the infrastructure secrets. A stack
# adopting pre-existing volumes (XPBUILDER_VOLUMES_EXTERNAL=true) keeps the
# EXISTING credentials verbatim so the adopted volumes keep working — those
# values may legitimately be shorter (e.g. postgres 'superset'), so only
# require them to be present and warn when they are below the normal floor.
if [ "$external" = "true" ]; then
    infra_min_len=1
    human_min_len=1
else
    infra_min_len=16
    human_min_len=8
fi

check_secret_min_length() {
    local key="$1" floor="$2" value
    value="$(value_of "$key")"
    if [ "${#value}" -lt "$floor" ]; then
        echo "ERROR: $key must contain at least $floor characters" >&2
        exit 1
    fi
    if [ "$external" = "true" ] && [ "${#value}" -lt 16 ]; then
        echo "WARNING: $key is shorter than 16 characters (preserved legacy credential)" >&2
    fi
}

for key in "${secret_keys[@]}"; do
    check_secret_min_length "$key" "$infra_min_len"
done
for key in "${human_secret_keys[@]}"; do
    check_secret_min_length "$key" "$human_min_len"
done

if [ "$(value_of MARIADB_PASSWORD)" = "$(value_of MARIADB_ROOT_PASSWORD)" ]; then
    echo "ERROR: MARIADB_PASSWORD and MARIADB_ROOT_PASSWORD must be different" >&2
    exit 1
fi

if [ "$(value_of SUPERSET_SECRET_KEY)" = "$(value_of GUEST_TOKEN_JWT_SECRET)" ]; then
    echo "ERROR: SUPERSET_SECRET_KEY and GUEST_TOKEN_JWT_SECRET must be different" >&2
    exit 1
fi

echo "XPBuilder .env validation passed for instance: $instance"
