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
- Bundled MariaDB: `MARIADB_DATABASE`, `MARIADB_USER`, `MARIADB_PASSWORD` (the
  credentials people sign in to phpMyAdmin with), `MARIADB_ROOT_PASSWORD`
  (provider-only), `XPBUILDER_MARIADB_VOLUME`, and
  `XPBUILDER_PHPMYADMIN_HOST_PORT` (loopback port for the reverse proxy).
- `XPBUILDER_VOLUMES_EXTERNAL=true` when adopting existing volumes.

## Provision the data

Pick one:

- **Adopt existing volumes** — set the exact external volume names in `.env`.
- **Restore from backup** — `bin/xpbuilder --env-file <env> backup` on the
  source, copy the snapshot, then `bin/xpbuilder --env-file <env> restore
  <backup>` on the target. A backup set contains **both** `superset-metadata.dump`
  and `uploads.dump` (the files uploaded through the UI): run the restore from
  the directory holding them so the upload store is recreated and reloaded too.
- **Start fresh** — run `bin/xpbuilder --env-file <env> init`, then connect
  the site's databases from the Superset UI.

## Enable file uploads on an existing stack

Superset only offers **Upload file to database** when at least one connection
has *Allow file uploads to database* enabled, so a stack created before uploads
were provisioned (or initialized with `XPBUILDER_ENABLE_FILE_UPLOADS=no`) greys
the menu out for everyone, administrators included. Converge it in place — no
rebuild and no restart:

```bash
# images built after this feature ship the provisioner:
docker exec -i <instance>_superset \
    /app/.venv/bin/python /opt/xpbuilder/bin/ensure_uploads_db.py

# older images: stream the same script from the repo checkout instead
cd /var/www/kot-xpbuilder
docker exec -i <instance>_superset \
    /app/.venv/bin/python - < docker/ensure_uploads_db.py
```

Then reload the Databases page: the upload menu is enabled once the API reports
at least one upload-capable connection. The script is idempotent and leaves
previously uploaded tables alone.

## Enable phpMyAdmin configuration storage on an existing stack

phpMyAdmin's bookmarks, table relations, and query history need a configuration
storage database that the site's database account cannot create. `bin/xpbuilder
init` and `upgrade` provision it automatically; on an older stack (or after the
MariaDB volume was replaced) converge it in place — no rebuild, no downtime:

```bash
cd /var/www/kot-xpbuilder
bin/xpbuilder --allow-group-env --env-file /path/to/site/.env pmadb
```

The script is idempotent, reads the schema from the running phpMyAdmin image,
and only creates what is missing.

## Bring it up

```bash
cd /var/www/kot-xpbuilder
bin/xpbuilder --env-file /path/to/site/.env config   # validate
bin/xpbuilder --env-file /path/to/site/.env up
bin/xpbuilder --env-file /path/to/site/.env health
python3 tests/contract/runtime_contract.py --base-url http://localhost:<port> \
    --username <admin> --password <password>
```

## Expose phpMyAdmin through a reverse proxy

The bundled phpMyAdmin is published on `127.0.0.1` only, so TLS termination at
the proxy is what makes it reachable from a browser. A ready-made vhost (with
the ACME challenge location) lives in `ops/nginx/`:

```bash
install -m 0644 ops/nginx/kot5phpmyadmin.openxpertz.com \
    /etc/nginx/sites-available/kot5phpmyadmin.openxpertz.com
ln -sf ../sites-available/kot5phpmyadmin.openxpertz.com \
    /etc/nginx/sites-enabled/kot5phpmyadmin.openxpertz.com
nginx -t && systemctl reload nginx

certbot certonly --webroot -w /var/www/letsencrypt \
    -d kot5phpmyadmin.openxpertz.com
certbot --nginx -d kot5phpmyadmin.openxpertz.com --redirect
```

Keep the vhost's `proxy_pass` port in step with
`XPBUILDER_PHPMYADMIN_HOST_PORT`, set
`XPBUILDER_PHPMYADMIN_URL=https://<host>/` (trailing slash required) so
phpMyAdmin builds correct asset URLs, and give the vhost a
`client_max_body_size` at least as large as the container's `UPLOAD_LIMIT`
(`XPBUILDER_PHPMYADMIN_UPLOAD_LIMIT`, default 512M) or large `.sql` imports fail
with HTTP 413. Apply `.env` changes with `bin/xpbuilder up` — Compose recreates
the phpMyAdmin container when its environment or port changes.

## Notes

- The image is stateless; all data lives in volumes. Never remove volumes with
  `down -v` unless you intend to wipe the stack.
- `bin/xpbuilder` is a bash wrapper around `docker compose` — it works
  identically on any Linux host with Docker; no other runtime is required.
- To expose Superset through a reverse proxy, terminate TLS at the proxy and
  forward to the site's host port; keep `XPBUILDER_ALLOWED_ORIGINS` in sync so
  embedded dashboards and CORS keep working.
- Uploaded files travel through the reverse proxy: raise the request body limit
  in the proxy site config (nginx defaults to 1 MB), for example
  `client_max_body_size 200m;` in the `server` block, otherwise larger CSV or
  Excel files fail with HTTP 413 before reaching Superset.
