# Site configurations

This runtime is single-site by default: the canonical configuration is the
protected file named exactly `.env` in the repository root (mode `0600`,
ignored by git).

```bash
bin/xpbuilder config    # validate ./.env and render the Compose model
bin/xpbuilder build     # build kot-xpbuilder:local from the vendored source
bin/xpbuilder init      # first-time metadata schema + admin
bin/xpbuilder up        # start or converge the stack
bin/xpbuilder health    # verify containers + Superset health endpoint
```

The local site binds host port **9010** (mapped to the container's internal
8088).

## Additional sites

Keep extra deployments as `instances/<site>/.env` files and pass them
explicitly. Each site needs a unique `XPBUILDER_INSTANCE`, host port, network,
and volume names:

```bash
bin/xpbuilder --env-file instances/other/.env up
```

Only files whose basename is exactly `.env` are accepted. When a reverse proxy
fronts a site, terminate TLS at the proxy and forward to the site's host port;
keep `XPBUILDER_ALLOWED_ORIGINS` in sync with the public URLs.
