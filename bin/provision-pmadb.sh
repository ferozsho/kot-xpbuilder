#!/usr/bin/env bash
# Provision phpMyAdmin's configuration storage (pmadb) inside the bundled
# MariaDB.
#
# phpMyAdmin keeps bookmarks, table relations, query history, and central
# columns in a dedicated database. The site's own database account cannot create
# it (it only has rights on its own database, by design), so the provider
# creates the storage database — from phpMyAdmin's own `create_tables.sql`, so
# the table layout always matches the running phpMyAdmin version — plus the
# small `pma` control account phpMyAdmin reaches those tables with.
#
# The control account never sees the site's data database, and the site's
# account never sees the storage database. Idempotent: `bin/xpbuilder init` and
# `upgrade` run it automatically, and it is safe to re-run on a live stack.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:?env file required}"
project="${2:?compose project required}"

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

control_database="$(value_of PHPMYADMIN_CONTROL_DATABASE)"
control_user="$(value_of PHPMYADMIN_CONTROL_USER)"
control_password="$(value_of PHPMYADMIN_CONTROL_PASSWORD)"
# A single quote in a site-chosen password must not break the statement.
control_password_sql="${control_password//\'/\'\'}"

compose=(docker compose --env-file "$env_file" -f "$root/compose.yml" -p "$project")

echo "Waiting for the bundled MariaDB to accept connections"
ready="no"
for _ in $(seq 1 60); do
    if "${compose[@]}" exec -T mariadb sh -c \
        "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root \
        -e \"SELECT 1\"" >/dev/null 2>&1; then
        ready="yes"
        break
    fi
    sleep 2
done
if [ "$ready" != "yes" ]; then
    echo "ERROR: the bundled MariaDB did not accept connections" >&2
    exit 1
fi

echo "Provisioning the phpMyAdmin configuration storage ($control_database)"
# phpMyAdmin's own schema file creates the database and its pma__* tables.
"${compose[@]}" exec -T phpmyadmin cat /var/www/html/sql/create_tables.sql \
    | "${compose[@]}" exec -T mariadb sh -c \
        "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root"

# The control account is used for the storage tables only.
"${compose[@]}" exec -T mariadb sh -c \
    "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root" <<SQL
CREATE USER IF NOT EXISTS '${control_user}'@'%' IDENTIFIED BY '${control_password_sql}';
ALTER USER '${control_user}'@'%' IDENTIFIED BY '${control_password_sql}';
GRANT SELECT, INSERT, UPDATE, DELETE, CREATE, ALTER, DROP, INDEX
    ON \`${control_database}\`.* TO '${control_user}'@'%';
GRANT SELECT ON \`mysql\`.* TO '${control_user}'@'%';
FLUSH PRIVILEGES;
SQL

# Report what phpMyAdmin will find: the pma__* tables plus the grant that lets
# the control account use them.
storage_tables="$("${compose[@]}" exec -T mariadb sh -c \
    "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root --skip-column-names --batch" \
    <<< "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = '${control_database}' AND table_name LIKE 'pma%';")"
storage_grants="$("${compose[@]}" exec -T mariadb sh -c \
    "exec env MYSQL_PWD=\"\$MARIADB_ROOT_PASSWORD\" mariadb -u root --skip-column-names --batch" \
    <<< "SELECT COUNT(*) FROM mysql.db WHERE Db = '${control_database}' AND User = '${control_user}';")"

if [ "${storage_tables:-0}" -lt 1 ] || [ "${storage_grants:-0}" -lt 1 ]; then
    echo "ERROR: the phpMyAdmin configuration storage is incomplete" >&2
    echo "       tables=${storage_tables:-0} grants=${storage_grants:-0}" >&2
    exit 1
fi

echo "phpMyAdmin configuration storage ready: $control_database ($storage_tables pma__ tables)"
