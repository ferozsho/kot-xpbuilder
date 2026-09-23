# Configuration

Every deployment uses one file named exactly `.env`. It is never committed and
must be mode `0600` or stricter. `bin/validate-env.sh` rejects duplicate,
missing, weak, or wildcard configuration before Docker Compose runs.

## Instance and routing

| Variable | Purpose |
| --- | --- |
| `XPBUILDER_INSTANCE` | Unique Compose project and container prefix |
| `XPBUILDER_HOST_PORT` | Browser-facing host port mapped to Superset 8088 |
| `XPBUILDER_INTERNAL_NETWORK` | Private network for web, worker, DB, and Redis |
| `XPBUILDER_ALLOWED_ORIGINS` | Comma-separated explicit origins allowed by CORS |
| `GUEST_TOKEN_JWT_AUDIENCE` | Public URL expected by embedded guest tokens |
| `XPBUILDER_APP_NAME` | Brand name shown in the UI (default `Advance BI`) |
| `XPBUILDER_BRAND_URL` | Optional URL the navbar brand logo links to (`/` = home) |

Each stack must have a unique instance name, host port, network, and volume
names. Two stacks may use the same image version but must not share mutable
volumes.

## Persistent storage

| Variable | Purpose |
| --- | --- |
| `XPBUILDER_METADATA_VOLUME` | Superset PostgreSQL metadata |
| `XPBUILDER_REDIS_VOLUME` | Redis cache, Celery broker, and beat schedule |
| `XPBUILDER_MARIADB_VOLUME` | Bundled MariaDB data (site data + phpMyAdmin) |
| `XPBUILDER_VOLUMES_EXTERNAL` | `true` only when adopting pre-existing volumes |

Set `XPBUILDER_VOLUMES_EXTERNAL=true` together with the exact existing volume
names to adopt volumes from an earlier deployment. The bundled MariaDB volume is
always Compose-managed, so enabling the database on a legacy stack cannot fail
on a missing external volume.

## Bundled MariaDB and phpMyAdmin

Every stack starts a MariaDB (`mariadb:11.4`) plus a phpMyAdmin front end. The
MariaDB never listens on a host port; only the stack's own containers can reach
it, and phpMyAdmin is published on the loopback interface for a TLS reverse
proxy (see [deployment.md](deployment.md)).

| Variable | Purpose |
| --- | --- |
| `MARIADB_DATABASE` | Database created on first start (letters, digits, underscore) |
| `MARIADB_USER` | Unprivileged account with full rights on that database |
| `MARIADB_PASSWORD` | Password of `MARIADB_USER` — also the phpMyAdmin login password |
| `MARIADB_ROOT_PASSWORD` | Container-local root secret (provider-only, in backups) |
| `XPBUILDER_PHPMYADMIN_HOST_PORT` | Loopback port mapped to phpMyAdmin (`127.0.0.1` only) |
| `XPBUILDER_PHPMYADMIN_URL` | Public phpMyAdmin URL with trailing `/` when a proxy fronts it (empty = auto-detect) |

`MARIADB_USER` / `MARIADB_PASSWORD` are human-facing credentials: they only have
to clear an 8-character floor, while `MARIADB_ROOT_PASSWORD` must reach 16.
phpMyAdmin uses cookie authentication, so no credentials are baked into the
container and the sign-in form takes the database credentials.

The database and user are created **on first start of an empty MariaDB volume**.
Changing `MARIADB_DATABASE` / `MARIADB_USER` later does not create the new
database or account — create them in phpMyAdmin (or with
`docker exec <instance>_mariadb mariadb -u root -p`) instead.

To analyze the bundled database in Superset, add a connection with the SQLAlchemy
URI (the stack's internal network resolves the service name `mariadb`):

```text
mysql+pymysql://<MARIADB_USER>:<MARIADB_PASSWORD>@mariadb:3306/<MARIADB_DATABASE>
```

phpMyAdmin accepts large imports: the reverse proxy must allow at least the
container's `UPLOAD_LIMIT` (`XPBUILDER_PHPMYADMIN_UPLOAD_LIMIT`, default 512M)
in `client_max_body_size`.

### Group-accessible `.env` files

`.env` must normally be mode `600`; `bin/validate-env.sh` rejects anything
readable or writable by group or other. Some hosted deployments deliberately
share the file with a restricted group (for example a jailed client workspace
that owns mode `660`), so provider commands take an explicit opt-in:

```bash
bin/xpbuilder --allow-group-env --env-file /var/www/kot-xpbuilder/.env health
```

`--allow-group-env` only relaxes the group bits; readable/writable by other
users is still rejected. Without the flag nothing changes.

## File uploads

| Variable | Purpose |
| --- | --- |
| `XPBUILDER_ENABLE_FILE_UPLOADS` | `no` skips provisioning the built-in upload database (default `yes`) |
| `XPBUILDER_UPLOAD_DB_NAME` | Name of the upload connection in Superset (default `File uploads`) |

`bin/xpbuilder init` runs `docker/ensure_uploads_db.py`, which creates the
`xpbuilder_uploads` role and database inside the metadata PostgreSQL instance
and registers a Superset connection with **Allow file uploads to database**
enabled and the `public` schema allow-listed. Uploaded CSV/Excel/Parquet files
are stored in that database, which lives in the metadata volume — size the
volume for the data a site uploads.

The role password is derived from `SUPERSET_SECRET_KEY` and is never written to
`.env`, so rotating `SUPERSET_SECRET_KEY` requires re-running the provisioner.
Re-running is always safe: the role, database, and connection are converged to
the expected state (and existing uploads are left untouched).

Uploaded tables are part of the backup set: `bin/xpbuilder backup` dumps the
upload store as `uploads.dump` (recorded in the manifest as `uploads_backup` /
`uploads_sha256`) and `bin/xpbuilder restore` recreates the role, database, and
connection before loading it back.

```bash
docker exec -i <instance>_superset \
    /app/.venv/bin/python /opt/xpbuilder/bin/ensure_uploads_db.py
```

Uploads are also gated by the `can_upload` permission on `Database`, which
upstream Superset grants to the **Admin** and **Alpha** roles only. Give a role
like **Gamma** that permission explicitly if its users should be able to upload
(see the README for the one-liner).

## Secrets

The following values are secrets and must exist only in `.env`:

- `SUPERSET_SECRET_KEY`
- `GUEST_TOKEN_JWT_SECRET`
- `SUPERSET_REDIS_PASSWORD`
- `POSTGRES_PASSWORD`
- `MARIADB_ROOT_PASSWORD`
- `SUPERSET_ADMIN_PASSWORD`
- `MARIADB_PASSWORD`

The matching usernames, database names, administrator identity, and origins are
also declared in `.env`. `bin/bootstrap-env.sh` generates all secrets; fresh
stacks require at least 16 characters for the infrastructure secrets
(`SUPERSET_SECRET_KEY`, `GUEST_TOKEN_JWT_SECRET`, `SUPERSET_REDIS_PASSWORD`,
`POSTGRES_PASSWORD`, `MARIADB_ROOT_PASSWORD`) and at least 8 for the
site-chosen, human-facing credentials (`SUPERSET_ADMIN_PASSWORD`,
`MARIADB_PASSWORD`).

Changing `SUPERSET_ADMIN_USERNAME` / `SUPERSET_ADMIN_PASSWORD` in `.env` only
affects future initializations — it never rewrites an existing account. To
change the credentials of a running stack's administrator, update the metadata
database as well:

```bash
# rename the account (keeps its id, role, and dashboard ownership)
docker exec <instance>_superset /app/.venv/bin/python -c "
from superset.app import create_app
app = create_app()
with app.app_context():
    from superset import security_manager, db
    user = security_manager.find_user(username='<old-username>')
    user.username = '<new-username>'
    db.session.commit()
"
# set the new password
docker exec <instance>_superset superset fab reset-password \
    --username <new-username> --password '<new-password>'
```

## Mutation gates

| Variable | Required value | Action enabled |
| --- | --- | --- |
| `XPBUILDER_ALLOW_INITIALIZE` | `yes` | First-time metadata initialization |
| `XPBUILDER_ALLOW_SCHEMA_UPGRADE` | `yes` | Superset metadata schema upgrade |
| `XPBUILDER_ALLOW_RESTORE` | `yes` | Destructive metadata restore |

Keep all three set to `no` during normal operation.

## Data sources

This runtime is Moodle-free: no database is registered during initialization.
Add data sources after startup through **Settings → Database Connections** in
the Superset UI (or `superset set-database-uri` inside the `superset`
container) — including the bundled MariaDB, which is reachable inside the stack
at `mariadb:3306` but is not registered automatically. Credentials for those
databases stay in Superset's encrypted metadata — never in `.env`, except for
the bundled MariaDB credentials that phpMyAdmin signs in with.
