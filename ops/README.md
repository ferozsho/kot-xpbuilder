# Client operations wrapper (`kot5`)

`kot5` is the single privileged entry point given to a hosted client (the
`kotbuilder` account on kot5). It always uses the **provider-approved** compose
definition, pins the infrastructure keys, and exposes only
`status|health|logs|up|down|restart|config|backup` — no `docker run`, no
container shells, no arbitrary sudo.

This file is the source of truth. Install or update it on the server with:

```bash
install -o root -g root -m 0755 ops/kot5 /usr/local/bin/kot5
```

Supporting files it depends on (server side):

| Path | Purpose |
| --- | --- |
| `/etc/kot5/compose.yml` | Provider-approved copy of `compose.yml` (keep in sync with this repo) |
| `/etc/kot5/pinned.env` | Pins instance/port/network/volume/image (plus the MariaDB root password, volume, and phpMyAdmin port/URL) so client `.env` edits cannot redirect the stack |
| `/etc/sudoers.d/kotbuilder` | `kotbuilder ALL=(root) NOPASSWD: /usr/local/bin/kot5 *` |
| `/var/www/kot-xpbuilder/.env` | Client-visible site configuration (mode `660`, group `kotbuilder`) |
| `/etc/nginx/sites-available/kot5phpmyadmin.openxpertz.com` | Public TLS vhost for phpMyAdmin (source of truth: `ops/nginx/`) |

Because that `.env` is deliberately group-readable, provider commands need the
explicit opt-in:

```bash
bin/xpbuilder --allow-group-env --env-file /var/www/kot-xpbuilder/.env health
```

`backup` dumps **both** the Superset metadata and the file-upload database, so
a restore cannot silently lose uploaded CSV/Excel data. Restores are a provider
action: pass the metadata dump to `bin/xpbuilder restore` and keep the sibling
`*uploads*.dump` in the same directory — it is restored automatically.

## Bundled MariaDB + phpMyAdmin

The stack ships its own MariaDB (site data managed in phpMyAdmin, analyzed in
Superset) and a phpMyAdmin front end on a **loopback-only** port. The client
signs in to phpMyAdmin with the `MARIADB_USER` / `MARIADB_PASSWORD` from their
`.env` (kot5: `admin` / the site password); the `MARIADB_ROOT_PASSWORD` stays a
provider secret and is pinned in `/etc/kot5/pinned.env`.

Provider actions when enabling or refreshing this on a client stack:

```bash
# 1. keep the approved compose and pinned keys current
install -o root -g root -m 0644 compose.yml /etc/kot5/compose.yml
# add to /etc/kot5/pinned.env (chmod 600):
#   XPBUILDER_MARIADB_VOLUME=<instance>_xpbuilder_mariadb
#   MARIADB_ROOT_PASSWORD=<generated 24-hex secret>
#   XPBUILDER_PHPMYADMIN_HOST_PORT=<free loopback port>
#   XPBUILDER_PHPMYADMIN_URL=https://<pma-host>/

# 2. publish phpMyAdmin through nginx + TLS
install -m 0644 ops/nginx/kot5phpmyadmin.openxpertz.com \
    /etc/nginx/sites-available/kot5phpmyadmin.openxpertz.com
ln -sf ../sites-available/kot5phpmyadmin.openxpertz.com \
    /etc/nginx/sites-enabled/kot5phpmyadmin.openxpertz.com
nginx -t && systemctl reload nginx
certbot certonly --webroot -w /var/www/letsencrypt \
    -d kot5phpmyadmin.openxpertz.com
certbot --nginx -d kot5phpmyadmin.openxpertz.com --redirect

# 3. converge the stack (client or provider)
kot5 up && kot5 health
```

`kot5 backup` writes three files per run: the metadata dump, the upload-store
dump, and `kot5-mariadb-<stamp>.dump` (a plain SQL dump of the bundled MariaDB).
`bin/xpbuilder restore` picks the MariaDB dump up automatically — through the
backup manifest or a sibling `*mariadb*.dump`.
