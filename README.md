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
| `backup` | Create a PostgreSQL metadata backup and manifest |
| `restore` | Restore a selected backup with explicit confirmation |

Configuration is documented in [docs/configuration.md](docs/configuration.md)
and server deployment in [docs/deployment.md](docs/deployment.md).

## Connecting your own data

No database is registered out of the box. Once the stack is healthy, add any
reachable SQL database from **Settings → Database Connections** (or
`superset set-database-uri`), then build datasets, charts, and dashboards in
the usual Superset way. The Report Designer reads the same datasets.

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
