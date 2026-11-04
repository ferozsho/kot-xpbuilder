# Develop without rebuilding the image

The runtime image bakes in three things: the Superset Python tree, the XP Prompt
service, and the compiled frontend. That is the right shape for a release and the wrong
shape for an afternoon of edits — a one-line change to `views.py` would cost a full
image build.

`compose.dev.yml` mounts the checkout over those three places instead, and `bin/dev`
wraps the few commands you actually need.

## One-time setup

```bash
# 1. Build the image once. DEV_MODE=true skips `npm ci` + webpack inside the
#    build, because the frontend comes from the checkout in dev mode.
bin/dev build

# 2. Start with the checkout mounted.
bin/dev up

# 3. Copy the two things the image compiles that the checkout does not have:
#    static/version_info.json and the translations/*.mo files.
bin/dev seed

# 4. Build the frontend once into superset/superset/static/assets.
bin/dev frontend
```

After that, the stack runs exactly as before — same image, same volumes — but it imports
your working tree.

## Day-to-day

| You changed | Run | Time |
| --- | --- | --- |
| `services/xp_prompt/**` (AI service) | `bin/dev reload` | ~2 s |
| `superset/superset/**` (views, commands, DAOs, config) | `bin/dev reload` | ~2 s |
| `superset/superset-frontend/**` | `bin/dev frontend` then refresh | ~30–90 s incremental |
| Lots of frontend edits | `bin/dev frontend watch`, keep it running | rebuild on save |
| `Dockerfile`, `requirements*`, `compose.yml` | `bin/dev build` + `bin/dev up` | a real build |
| A new migration | `bin/dev migrate` | ~2 s |
| New API routes/permissions | `bin/dev permissions` | ~5 s |

`bin/dev reload` restarts the five Python services together
(`superset`, `superset-worker`, `superset-beat`, `xp-prompt-api`, `xp-prompt-worker`),
because a change to a shared module is usually needed by more than one of them.

## Why each piece works

* **Python** — the image installs Superset with `uv pip install -e .` and builds the XP
  Prompt service at `/app/xp_prompt` with `PYTHONPATH=/app/xp_prompt`. Both therefore
  import from the mounted directory, so a restart is enough. No `.pyc` staleness:
  Python compares source mtimes.
* **Frontend** — this is the expensive one, and it is *not* hot-reloaded. Webpack writes
  to `superset/superset/static/assets` (`BUILD_DIR` in `webpack.config.js`), which is
  inside the mounted `superset/superset` tree, so the running Superset serves the new
  bundles on the next browser load. `bin/dev frontend` runs webpack in a `node:20` container
  with `node_modules` in a named volume, which is what makes the second build fast.
* **Assets must exist** — mounting `superset/superset` over `/app/superset` hides the
  bundles baked into the image. `bin/dev up` says so if
  `superset/superset/static/assets` is empty, and the UI will have no JS until you run
  `bin/dev frontend` once.
* **Translations and version metadata** — the image compiles `.po` → `.mo` and writes
  `static/version_info.json`. The checkout has neither, so `bin/dev seed` copies them out
  of the running container. Both are already gitignored; if you skip this step the UI
  falls back to English and the version string comes from `package.json`.

## Things that still need a rebuild

* Anything copied into the image *outside* these trees: `config/superset_config.py`
  (baked at `/app/xpbuilder/`), `docker/entrypoints`, `docker/*.py` helpers, the
  `requirements.lock` install, and the patches under `docker/patches`.
* Java/geospatial extras, Playwright browsers, and the `xp_prompt` dependencies — those
  are image layers, not mounts.

If you find yourself rebuilding for a file that is not in the list above, mount it too:
add it to `compose.dev.yml` and it becomes a restart.

## Guard rails

* **Never run a bare `docker compose up -d`.** Without `-f compose.dev.yml` the services
  revert to the baked image, and your edits silently disappear from the running stack.
  Use `bin/dev up`.
* **`bin/dev` with no argument prints this help** — it does not start anything. (An
  earlier version defaulted to `up`, which recreated live containers by accident.)
* **Readiness pins the schema revision.** The XP Prompt service reports `unhealthy` when
  `alembic_version` differs from `xp_prompt/schema.py:SCHEMA_REVISION`, so after pulling a
  migration run `bin/dev migrate` *after* the containers are up.
* In dev mode a code change is visible to *every* user of the stack, including a client
  tenant if you are running one. Point `XPBUILDER_INSTANCE` at a scratch project (and
  `XPBUILDER_HOST_PORT` at another port) when that matters.

## Going back to the baked image

The overlay only applies when you pass the file, so reverting is one command (no
`bin/dev` verb, because this must be explicit):

```bash
docker compose --env-file .env -f compose.yml -p "$(sed -n 's/^XPBUILDER_INSTANCE=//p' .env)" up -d
```

That recreates the five Python services without the mounts — i.e. exactly what runs on a
customer server. `docker inspect <container> --format '{{len .Mounts}}'` should then
report `0`.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| Blank page, 404s on `/static/assets/*` | assets dir empty | `bin/dev frontend` |
| Python change had no effect | container imported the old module | `bin/dev reload` |
| `unhealthy` on `xp_prompt_api` | schema revision mismatch | `bin/dev migrate` |
| `403 Access Denied` on the new APIs | FAB permissions not granted | `bin/dev permissions` |
| UI in English only | `.mo` files were never seeded | `bin/dev seed`, then reload |
| Webpack OOM | default node heap | already `--max_old_space_size=8192`; give Docker more RAM |
