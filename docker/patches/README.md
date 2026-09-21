# Vendored Superset source patches

The XPBuilder runtime is built from the vendored Apache Superset source in
[`../superset`](../../superset) (tag `6.1.0`). Custom code changes that you
want baked into the image live here as unified diffs.

The vendored `superset/` tree has **no `.git` of its own** — a nested repository
would make the xpbuilder repo treat `superset/` as a submodule. Instead the
pristine 6.1.0 baseline lives in a separate repository at
`~/.cache/xpbuilder/superset-baseline.git` (dev-machine only; override with
`XPBUILDER_SUPERSET_GIT`), whose `core.worktree` points at `superset/`.
Edit files inside `superset/`, then export your edits with
[`new-patch.sh`](new-patch.sh) (also available as `bin/xpbuilder patch`).

## Workflow

```bash
# 1. Make your edit inside the vendored source
#    (e.g. superset/superset/views/core.py)

# 2. Export it as a patch + verify it applies to the pristine baseline
bin/xpbuilder patch 0001-my-change
# or: docker/patches/new-patch.sh 0001-my-change

# 3. Optional: verify every patch in the tree still applies
bin/xpbuilder patch --verify
```

`new-patch.sh` uses `git diff HEAD` (including new files), writes
`docker/patches/<name>.patch`, and dry-runs it against the pristine baseline
before reporting success.

## How it works

1. The build's `superset-src` stage copies the vendored `superset/` tree and
   runs [`apply.sh`](apply.sh).
2. Every `*.patch` file in this directory is applied with
   `patch -p1 -N --batch` (paths relative to the `superset/` root).
3. Later build stages copy the **patched** tree, so patches are baked into the
   final image.

## Rules

- Always generate patches with `new-patch.sh` (or `git diff`/`diff -u`) so
  hunks are in the canonical unified format — hand-written hunks are fragile
  and will abort the build.
- Name patches with a zero-padded numeric prefix so they apply in order.
- Patches must apply cleanly to a pristine 6.1.0 tree; `apply.sh` aborts the
  build if one fails. Re-applying an already-applied patch is skipped.
- **Not patchable**: `superset-frontend/package.json` and
  `superset-frontend/package-lock.json` are bind-mounted directly from the
  build context for the `npm ci` cache and bypass the patch stage. Patch
  everything else (frontend source, Python, templates, static assets).
- **NOTE (2026-08-27)**: the Report Designer feature code is committed directly
  in this repo (see `superset/superset/views/report_designer/*`,
  `superset/superset-frontend/src/features/reportDesigner/*`), so the former
  `0001-fix-report-list-table-ui.patch` was REMOVED — it became stale the
  moment the code was committed and aborted the build (neither forward nor
  reverse applied once the files evolved). Do NOT re-add it. If you change
  report designer code, commit the change directly (no patch needed).

## After adding a patch

Rebuild the image (`bin/xpbuilder --env-file <env> build`) and redeploy. The
full build takes 30-60 minutes.
