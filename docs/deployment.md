# Deploy XPBuilder to a new server

XPBuilder is a standalone, stateless Superset runtime. The image
(`kot-xpbuilder:local`) is env-driven: the baked
`config/superset_config.py` reads every site-specific value from the per-site
`.env`, so the **same image runs on any server**. Only two things are
machine-specific and must be provisioned per site:

1. The per-site `.env` (project name, host port, origins, secrets).
2. The data volumes — PostgreSQL metadata (dashboards/charts/datasets) and
   Redis — which live in volumes, **not** in the image.

## What travels with the image

| Item | Where it lives | Notes |
| --- | --- | --- |
| Superset runtime + branding | In the image (baked) | Built from the vendored Apache Superset 6.1.0 source in `superset/` |
| Superset config | In the image (baked) | `config/superset_config.py` reads env vars only |
| Per-site settings | `.env` on the server | `XPBUILDER_INSTANCE`, host port, origins, secrets |
| Metadata (dashboards, charts, datasets) | External postgres volume | Adopt, restore from backup, or start fresh |
| Redis data | External volume | Adopt or recreate |
| Builder CLI + compose | Repo on the server | `bin/xpbuilder`, `compose.yml`, `docker/` |

## Option A — pull a prebuilt image from a registry

1. On the source server, tag and push the image:
   ```bash
   docker tag kot-xpbuilder:local ghcr.io/<org>/kot-xpbuilder:6.1.0-kot1
   docker push ghcr.io/<org>/kot-xpbuilder:6.1.0-kot1
   ```
2. On the target server, pull it and retag to the name the compose file uses:
   ```bash
   docker pull ghcr.io/<org>/kot-xpbuilder:6.1.0-kot1
   docker tag ghcr.io/<org>/kot-xpbuilder:6.1.0-kot1 kot-xpbuilder:local
   ```
3. Clone the builder repo so `bin/xpbuilder` and `compose.yml` are available:
   ```bash
   git clone git@github.com:ferozsho/kot-xpbuilder.git /var/www/kot-xpbuilder
   ```

## Option B — offline transfer (air-gapped)

```bash
docker save kot-xpbuilder:local -o kot-xpbuilder-image.tar
# copy the tarball to the target server, then:
docker load -i kot-xpbuilder-image.tar
git clone git@github.com:ferozsho/kot-xpbuilder.git /var/www/kot-xpbuilder
```

## Option C — build from the repo (recommended for parity)

Because the runtime is built from the vendored source tree, building anywhere
produces an equivalent image.

```bash
cd /var/www/kot-xpbuilder
git pull            # keep the builder + compose current
bin/xpbuilder --env-file /path/to/site/.env build
```

## Provision the per-site `.env`

Copy the shape of the repo-root `.env` (chmod 600) and set:

- `XPBUILDER_INSTANCE` — unique project name (e.g. `kot`).
- Host port for Superset (internal is always 8088) and
  `XPBUILDER_ALLOWED_ORIGINS` for any site that embeds Superset.
- `SUPERSET_SECRET_KEY` / `GUEST_TOKEN_JWT_SECRET` — preserve existing values
  when adopting volumes; a new key cannot decrypt passwords already stored in
  the Superset metadata, and `.env` does not change an existing admin password.
- PostgreSQL role/password, Superset admin user/password.
- `XPBUILDER_VOLUMES_EXTERNAL=true` when adopting existing volumes.

## Provision the data

Pick one:

- **Adopt existing volumes** — set the exact external volume names in `.env`.
- **Restore from backup** — `bin/xpbuilder --env-file <env> backup` on the
  source, copy the snapshot, then `bin/xpbuilder --env-file <env> restore
  <backup>` on the target.
- **Start fresh** — run `bin/xpbuilder --env-file <env> init`, then connect
  the site's databases from the Superset UI.

## Bring it up

```bash
cd /var/www/kot-xpbuilder
bin/xpbuilder --env-file /path/to/site/.env config   # validate
bin/xpbuilder --env-file /path/to/site/.env up
bin/xpbuilder --env-file /path/to/site/.env health
python3 tests/contract/runtime_contract.py --base-url http://localhost:<port> \
    --username <admin> --password <password>
```

## Notes

- The image is stateless; all data lives in volumes. Never remove volumes with
  `down -v` unless you intend to wipe the stack.
- `bin/xpbuilder` is a bash wrapper around `docker compose` — it works
  identically on any Linux host with Docker; no other runtime is required.
- To expose Superset through a reverse proxy, terminate TLS at the proxy and
  forward to the site's host port; keep `XPBUILDER_ALLOWED_ORIGINS` in sync so
  embedded dashboards and CORS keep working.
