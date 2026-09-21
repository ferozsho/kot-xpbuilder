#!/usr/bin/env python3
"""Provision the XPBuilder database that uploaded files are stored in.

Superset only offers "Upload file to database" when at least one database
connection has ``allow_file_upload`` enabled, and a fresh XPBuilder stack has
no connections at all, so the upload menu stays greyed out for everyone.

This script is idempotent and safe to re-run at any time. It:

1. creates an unprivileged PostgreSQL role plus a dedicated
   ``xpbuilder_uploads`` database inside the existing metadata PostgreSQL
   instance, so uploaded files never land in (and cannot reach) Superset's
   own metadata database;
2. registers or updates the matching Superset connection with file uploads
   enabled and the ``public`` schema allow-listed;
3. prints the connection it converged on.

Run it inside a running XPBuilder container (``initialize.sh`` does this for
new stacks):

    docker exec -i <instance>_superset \\
        /app/.venv/bin/python /opt/xpbuilder/bin/ensure_uploads_db.py

Environment (all supplied by compose.yml):

    POSTGRES_USER, POSTGRES_PASSWORD   metadata instance credentials
    POSTGRES_DB                        metadata database name
    SUPERSET_SECRET_KEY                derives the upload role password
    XPBUILDER_UPLOAD_DB_NAME           connection name, default "File uploads"
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
from urllib.parse import quote_plus

import psycopg2

UPLOAD_ROLE = 'xpbuilder_uploads'
UPLOAD_DATABASE = 'xpbuilder_uploads'
UPLOAD_SCHEMAS = ['public']
UPLOAD_HOST = 'superset-db'
UPLOAD_PORT = 5432
DEFAULT_CONNECTION_NAME = 'File uploads'


def require(name: str) -> str:
    value = os.environ.get(name, '').strip()
    if not value:
        raise SystemExit(f'ERROR: {name} must be set (see docs/configuration.md)')
    return value


def optional(name: str, default: str) -> str:
    return os.environ.get(name, '').strip() or default


def upload_role_password(secret_key: str) -> str:
    """Derive a stable password for the upload role from the stack secret.

    Deterministic, so re-running never invalidates the registered connection,
    and independent of ``POSTGRES_PASSWORD``, so learning the upload
    credentials cannot be reused to log in as the metadata superuser.
    Rotating ``SUPERSET_SECRET_KEY`` changes this password; re-run the script
    (``bin/xpbuilder init`` / the one-liner above) after rotating it.
    """
    digest = hmac.new(
        secret_key.encode(), b'xpbuilder-uploads-role:v1', hashlib.sha256
    ).digest()
    return base64.urlsafe_b64encode(digest).decode().rstrip('=')


def ensure_role_and_database(password: str) -> None:
    """Create the upload role and database inside the metadata instance."""
    metadata_db = require('POSTGRES_DB')
    if UPLOAD_DATABASE == metadata_db:
        raise SystemExit(
            'ERROR: the upload database must differ from the metadata database'
        )

    # Deliberately not used as a context manager: psycopg2 starts a
    # transaction as soon as the connection is entered, and CREATE DATABASE
    # cannot run inside a transaction block. Autocommit goes on first.
    connection = psycopg2.connect(
        host=UPLOAD_HOST,
        port=UPLOAD_PORT,
        user=require('POSTGRES_USER'),
        password=require('POSTGRES_PASSWORD'),
        dbname=metadata_db,
    )
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        cursor.execute(
            'SELECT 1 FROM pg_roles WHERE rolname = %s', (UPLOAD_ROLE,)
        )
        privileges = (
            'WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD %s'
        )
        if cursor.fetchone():
            cursor.execute(f'ALTER ROLE {UPLOAD_ROLE} {privileges}', (password,))
        else:
            cursor.execute(f'CREATE ROLE {UPLOAD_ROLE} {privileges}', (password,))

        cursor.execute(
            'SELECT pg_get_userbyid(datdba) FROM pg_database'
            ' WHERE datname = %s',
            (UPLOAD_DATABASE,),
        )
        owner = cursor.fetchone()
        if owner is None:
            cursor.execute(
                f'CREATE DATABASE {UPLOAD_DATABASE} OWNER {UPLOAD_ROLE}'
            )
        elif owner[0] != UPLOAD_ROLE:
            cursor.execute(
                f'ALTER DATABASE {UPLOAD_DATABASE} OWNER TO {UPLOAD_ROLE}'
            )
    finally:
        connection.close()


def ensure_connection(uri: str, connection_name: str) -> str:
    """Create or update the Superset connection that receives uploads."""
    from superset.app import create_app

    # The app must be created before superset.models is imported: the encrypted
    # field factory used by Database only exists once the app is initialized.
    app = create_app()
    with app.app_context():
        from superset import db
        from superset.models.core import Database

        database = (
            db.session.query(Database)
            .filter(Database.database_name == connection_name)
            .one_or_none()
        )
        if database is None:
            database = Database(database_name=connection_name)
            db.session.add(database)

        # Stores the password encrypted and keeps the URI in the masked form
        # the Superset UI expects (password field lives in dbs.password).
        database.set_sqlalchemy_uri(uri)
        database.allow_file_upload = True
        database.expose_in_sqllab = True
        database.extra = json.dumps(
            {
                'metadata_params': {},
                'engine_params': {},
                'metadata_cache_timeout': {},
                'schemas_allowed_for_file_upload': UPLOAD_SCHEMAS,
            }
        )
        db.session.commit()
        return database.safe_sqlalchemy_uri()


def main() -> int:
    password = upload_role_password(require('SUPERSET_SECRET_KEY'))
    ensure_role_and_database(password)

    uri = (
        f'postgresql+psycopg2://{UPLOAD_ROLE}:{quote_plus(password)}'
        f'@{UPLOAD_HOST}:{UPLOAD_PORT}/{UPLOAD_DATABASE}'
    )
    connection_name = optional('XPBUILDER_UPLOAD_DB_NAME', DEFAULT_CONNECTION_NAME)
    masked_uri = ensure_connection(uri, connection_name)

    print(
        f'File uploads enabled: connection "{connection_name}" -> {masked_uri}'
        f' (schemas: {", ".join(UPLOAD_SCHEMAS)})'
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())
