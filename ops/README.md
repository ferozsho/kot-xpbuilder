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
| `/etc/kot5/pinned.env` | Pins instance/port/network/volume/image so client `.env` edits cannot redirect the stack |
| `/etc/sudoers.d/kotbuilder` | `kotbuilder ALL=(root) NOPASSWD: /usr/local/bin/kot5 *` |
| `/var/www/kot-xpbuilder/.env` | Client-visible site configuration (mode `660`, group `kotbuilder`) |

Because that `.env` is deliberately group-readable, provider commands need the
explicit opt-in:

```bash
bin/xpbuilder --allow-group-env --env-file /var/www/kot-xpbuilder/.env health
```

`backup` dumps **both** the Superset metadata and the file-upload database, so
a restore cannot silently lose uploaded CSV/Excel data. Restores are a provider
action: pass the metadata dump to `bin/xpbuilder restore` and keep the sibling
`*uploads*.dump` in the same directory — it is restored automatically.
