# Kot XPBuilder

Standalone Apache Superset runtime for Kot Advanced BI dashboards.

This is the XPBuilder runtime **without Moodle integration**: no connector
plugin, no external Moodle network, and no read-only MariaDB replica. Superset,
its PostgreSQL metadata store, and Redis are the whole stack; data sources are
attached through Superset's own "Connect a database" flow, so any reachable SQL
database can be analyzed without touching Moodle.

The runtime is built from the vendored Apache Superset 6.1.0 source tree in
`superset/` together with the fork features (Report Designer, branding,
chart/number format fixes, "Clear all" native-filter fix).

## Safety rules

- The runtime is built from the vendored source tree in `superset/` (Apache
  Superset 6.1.0) — never from a prebuilt registry image. Custom source changes
  are committed directly into `superset/` or exported to `docker/patches/` and
  are baked into the image.
- Never place credentials in Compose, this README, or any other tracked file.
  Each deployment reads secrets from a file named exactly `.env` (mode `0600`).
- Never run first-time initialization against an existing metadata volume.
- Never remove volumes during normal `down` or rollback operations.

## Quick start

Create an isolated local configuration:

```bash
bin/bootstrap-env.sh --instance kot --host-port 9010
```

Review `.env`, then run:

```bash
bin/xpbuilder config
bin/xpbuilder build
bin/xpbuilder init
bin/xpbuilder up
bin/xpbuilder health
```

Open <http://localhost:9010> and sign in with the `SUPERSET_ADMIN_*`
credentials from `.env`. Set `XPBUILDER_ALLOW_INITIALIZE` back to `no` after a
new stack is initialized; existing stacks must be started with `up` and must
not be initialized again.

Use a site-specific `.env` outside the checkout with:

```bash
bin/xpbuilder --env-file /path/to/site/.env up
```

Only files whose basename is exactly `.env` are accepted.

## Layout

| Path | Purpose |
| --- | --- |
| `compose.yml` | Superset web, Celery worker/beat, PostgreSQL metadata, Redis |
| `config/superset_config.py` | Baked runtime configuration (env-driven only) |
| `customizations/` | Branding images + tail-JS (logo link, "Clear all" fix) |
| `docker/initialize.sh` | First-run metadata schema, admin, role sync |
| `docker/ensure_uploads_db.py` | Idempotent provisioning of the built-in file-upload database |
| `ops/kot5` | Privileged ops wrapper handed to a hosted client (see `ops/README.md`) |
| `docker/patches/` | Export/verify vendored-source edits as patches |
| `bin/xpbuilder` | CLI wrapper around `docker compose` |
| `instances/<site>/.env` | One protected configuration per site (never committed) |
| `superset/` | Vendored Apache Superset 6.1.0 source (build input) |

## Commands

| Command | Purpose |
| --- | --- |
| `config` | Validate `.env` and render the resolved Compose model |
| `build` | Build the XPBuilder image from the vendored Superset source |
| `patch` | Export/verify Superset source edits as `docker/patches/*.patch` |
| `init` | Initialize a brand-new metadata database explicitly |
| `upgrade` | Run an explicitly enabled Superset schema upgrade |
| `up` | Start or converge the site instance |
| `down` | Stop containers without deleting volumes |
| `ps` | Show the actual Compose services, state, and ports |
| `health` | Verify container health and the Superset health endpoint |
| `backup` | Create a metadata + uploaded-data backup and manifest |
| `restore` | Restore a selected backup with explicit confirmation |

Configuration is documented in [docs/configuration.md](docs/configuration.md)
and server deployment in [docs/deployment.md](docs/deployment.md).

## Connecting your own data

A new stack is initialized with one ready-to-use connection so files can be
uploaded immediately. Add any other reachable SQL database from **Settings →
Database Connections** (or `superset set-database-uri`), then build datasets,
charts, and dashboards in the usual Superset way. The Report Designer reads the
same datasets.

### Uploading files (CSV, Excel, Parquet)

`bin/xpbuilder init` provisions a built-in **File uploads** database:

- a dedicated `xpbuilder_uploads` PostgreSQL database inside the metadata
  instance, owned by the unprivileged `xpbuilder_uploads` role (uploaded data
  never lives in, and cannot reach, Superset's own metadata database);
- a Superset connection named by `XPBUILDER_UPLOAD_DB_NAME` with **Allow file
  uploads to database** enabled and the `public` schema allow-listed.

Files are then imported from **Databases ‣ Upload file to database** (or
**+ ‣ Upload CSV/Excel/Columnar**), which creates a table in that database plus
a dataset that charts can be built on.

Superset only offers the upload menu when at least one connection accepts file
uploads; without one the menu entries stay greyed out for every user, including
administrators. Uploads are also gated by the `can_upload` permission on
`Database`, which upstream Superset grants to **Admin** and **Alpha** only —
users with the Gamma role do not see the menu. Grant it deliberately if a site
wants non-admin uploads:

```bash
docker exec <instance>_superset /app/.venv/bin/python -c "
from superset.app import create_app
app = create_app()
with app.app_context():
    sm = app.appbuilder.sm
    sm.add_permission_role(sm.find_role('Gamma'), sm.add_permission_view_menu('can_upload', 'Database'))
"
```

Set `XPBUILDER_ENABLE_FILE_UPLOADS=no` to skip provisioning the built-in
database, and enable uploads on an external connection instead (its **Advanced →
Security** section has the **Allow file uploads to database** checkbox).

Uploaded tables live in that database, so `bin/xpbuilder backup` dumps it
alongside the Superset metadata (`uploads.dump` + `uploads_sha256` in the
manifest) and `bin/xpbuilder restore` loads it back — otherwise a restore would
silently drop the client's uploaded data. Backups taken by the hosted-client
wrapper (`ops/kot5 backup`) contain the same two dumps, and a restore picks up
any sibling `*uploads*.dump`, so both sources restore with one command.

## Compatibility

The machine-readable contract is [compatibility.json](compatibility.json): the
runtime is declared `standalone` (`moodle_integration: false`) and pins Apache
Superset 6.1.0, built from the vendored source tree.

## Development

```bash
tests/static.sh
tests/integration.sh
```

The integration test uses a uniquely named temporary Compose project and
volumes, then removes only those test resources.

## Building from source

The runtime is built from the vendored Apache Superset source in `superset/`
(a shallow clone of tag `6.1.0`; its pristine baseline lives in a separate repo
at `~/.cache/xpbuilder/superset-baseline.git`, dev-machine only — see
`docker/patches/README.md`). The root `Dockerfile` is the upstream multi-stage
build adapted to that layout, plus the `xpbuilder` overlay stage (config,
branding, bootstrap). A full build takes 30-60 minutes and needs BuildKit
(Docker 23+).

- Fork source changes are committed directly into `superset/`. To export a
  change set separately, use
  `docker/patches/new-patch.sh <name>` (applied during the build's
  `superset-src` stage).
- Runtime overlay (config, branding, init): `config/`, `customizations/`,
  `docker/initialize.sh`, `requirements.lock`.
- To upgrade the vendored version: re-clone the new tag over `superset/`,
  delete its `.git`, update `superset/.xpbuilder-vendor` and
  `compatibility.json`, then rebuild.
- Do not commit `node_modules/` or build artifacts inside `superset/`.

## Reflecting code changes locally (dev workflow)

After editing anything under `superset/` (frontend or Python backend), the
running stack does not pick it up automatically. Rebuild and recreate:

```bash
docker compose --env-file .env build --build-arg DEV_MODE=false superset
docker compose --env-file .env up -d superset superset-worker superset-beat
```

Then hard-refresh the browser (**Ctrl+Shift+R**). Backend-only changes can be
built without `--build-arg DEV_MODE=false` (that flag only controls the
frontend webpack build).
