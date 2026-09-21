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
| `XPBUILDER_VOLUMES_EXTERNAL` | `true` only when adopting pre-existing volumes |

Set `XPBUILDER_VOLUMES_EXTERNAL=true` together with the exact existing volume
names to adopt volumes from an earlier deployment.

## Secrets

The following values are secrets and must exist only in `.env`:

- `SUPERSET_SECRET_KEY`
- `GUEST_TOKEN_JWT_SECRET`
- `SUPERSET_REDIS_PASSWORD`
- `POSTGRES_PASSWORD`
- `SUPERSET_ADMIN_PASSWORD`

The matching usernames, database name, administrator identity, and origins are
also declared in `.env`. `bin/bootstrap-env.sh` generates all secrets; fresh
stacks require at least 16 characters for the infrastructure secrets
(`SUPERSET_SECRET_KEY`, `GUEST_TOKEN_JWT_SECRET`, `SUPERSET_REDIS_PASSWORD`,
`POSTGRES_PASSWORD`) and at least 8 for `SUPERSET_ADMIN_PASSWORD`, which is a
site-chosen, human-facing credential.

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
container). Credentials for those external databases stay in Superset's
encrypted metadata — never in `.env`.
