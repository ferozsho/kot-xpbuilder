#!/usr/bin/env bash

set -euo pipefail

required() {
    local name="$1"
    if [ -z "${!name:-}" ]; then
        echo "ERROR: $name must be provided through .env" >&2
        exit 1
    fi
}

for name in \
    POSTGRES_USER POSTGRES_PASSWORD POSTGRES_DB; do
    required "$name"
done

mode="${1:-}"
case "$mode" in
    new)
        if [ "${XPBUILDER_ALLOW_INITIALIZE:-no}" != "yes" ]; then
            echo "ERROR: new-stack initialization requires XPBUILDER_ALLOW_INITIALIZE=yes in .env" >&2
            exit 1
        fi
        for name in SUPERSET_ADMIN_USERNAME SUPERSET_ADMIN_PASSWORD \
            SUPERSET_ADMIN_FIRSTNAME SUPERSET_ADMIN_LASTNAME SUPERSET_ADMIN_EMAIL; do
            required "$name"
        done
        ;;
    upgrade)
        if [ "${XPBUILDER_ALLOW_SCHEMA_UPGRADE:-no}" != "yes" ]; then
            echo "ERROR: schema upgrade requires XPBUILDER_ALLOW_SCHEMA_UPGRADE=yes in .env" >&2
            exit 1
        fi
        ;;
    *)
        echo "Usage: initialize.sh new|upgrade" >&2
        exit 2
        ;;
esac

echo "Applying the Apache Superset metadata schema for mode: $mode"
superset db upgrade

if [ "$mode" = "new" ]; then
    echo "Creating the initial XPBuilder administrator"
    if ! superset fab create-admin \
        --username "$SUPERSET_ADMIN_USERNAME" \
        --password "$SUPERSET_ADMIN_PASSWORD" \
        --firstname "$SUPERSET_ADMIN_FIRSTNAME" \
        --lastname "$SUPERSET_ADMIN_LASTNAME" \
        --email "$SUPERSET_ADMIN_EMAIL"; then
        echo "Administrator creation returned non-zero; it may already exist. Continuing with role synchronization."
    fi
fi

echo "Synchronizing Superset roles and permissions"
superset init

if [ "$mode" = "new" ]; then
    /app/.venv/bin/python - <<'PY'
import os

import psycopg2

connection = psycopg2.connect(
    host='superset-db',
    port=5432,
    user=os.environ['POSTGRES_USER'],
    password=os.environ['POSTGRES_PASSWORD'],
    dbname=os.environ['POSTGRES_DB'],
)
try:
    with connection.cursor() as cursor:
        cursor.execute(
            'SELECT 1 FROM ab_user WHERE username = %s',
            (os.environ['SUPERSET_ADMIN_USERNAME'],),
        )
        if cursor.fetchone() is None:
            raise RuntimeError('initial XPBuilder administrator was not created')
finally:
    connection.close()
PY
fi

echo "Ensuring the embedded Gamma role can persist dashboard colors"
/app/.venv/bin/python - <<'PY'
from superset.app import create_app

app = create_app()
with app.app_context():
    security_manager = app.appbuilder.sm
    permission_view = security_manager.add_permission_view_menu(
        'can_put_colors',
        'Dashboard',
    )
    gamma = security_manager.find_role('Gamma')
    if gamma is None or permission_view is None:
        raise RuntimeError('could not resolve Gamma or Dashboard.can_put_colors')
    security_manager.add_permission_role(gamma, permission_view)
PY

# Without an upload-capable database connection, Superset greys out
# "Upload file to database" for every user, including administrators.
if [ "${XPBUILDER_ENABLE_FILE_UPLOADS:-yes}" = "no" ]; then
    echo "Skipping the built-in file upload database (XPBUILDER_ENABLE_FILE_UPLOADS=no)"
else
    echo "Provisioning the built-in file upload database"
    /app/.venv/bin/python /opt/xpbuilder/bin/ensure_uploads_db.py
fi

echo "XPBuilder initialization completed successfully"
