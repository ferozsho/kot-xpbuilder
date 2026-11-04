# Release, upgrade and rollback (P11-F09)

Every command here is the one this repository actually uses. Nothing is
illustrative: if a step is not reproducible, it does not belong in a release.

## Before a release

```bash
cd /home/administrator/kot-xpbuilder
python3 tests/contract/check_compatibility.py          # contract + schema audit
docker compose exec -T xp-prompt-api sh -lc \
  'cd /app/xp_prompt && python -m pytest tests/ -q'    # backend suite
bin/backup.sh .env kot /var/backups/xpbuilder          # before anything changes
bin/verify-backup.sh .env kot /var/backups/xpbuilder/<timestamp>
```

A release with a failing contract or an unverified backup does not proceed. The
verify step is not optional: a dump file that has never been restored is a
guess.

## Applying a release

1. **Code.** Pull the revision, then rebuild what changed:

   ```bash
   docker compose build superset xp-prompt-api xp-prompt-worker xp-prompt-beat
   ```

2. **Migrations.** Schema first, service second — the new code expects the new
   shape.

   ```bash
   docker compose exec -T xp-prompt-api sh -lc \
     'cd /app/xp_prompt && /app/.venv/bin/python -m alembic upgrade head'
   ```

   `SCHEMA_REVISION` in `xp_prompt/schema.py` must equal the new head. If it does
   not, `/health/ready` reports `postgres: unavailable` and the deployment is
   *not* ready — that mismatch is the first thing to check.

3. **Restart the runtime.**

   ```bash
   docker compose up -d superset xp-prompt-api xp-prompt-worker xp-prompt-beat
   ```

4. **Frontend assets** (operator step; there is no local toolchain in the dev image):

   ```bash
   bin/dev frontend
   ```

5. **Verify.**

   ```bash
   docker compose exec -T xp-prompt-api sh -lc 'curl -s http://127.0.0.1:8090/health/ready'
   docker compose exec -T xp-prompt-api sh -lc 'curl -s http://127.0.0.1:8090/metrics | head'
   docker compose exec -T superset sh -lc \
     'for p in settings health usage alerts; do \
        curl -s -o /dev/null -w "$p %{http_code}\n" \
        "http://127.0.0.1:8088/aiprompt/api/$p/"; done'
   ```

   Expect `ready` for all three dependencies, metrics to render, and `401` (route
   exists, unauthenticated) rather than `404` from the proxy.

6. **Permissions.** A release that adds a route needs its capability:

   ```bash
   docker compose cp docker/ensure_ai_prompt_permissions.py \
     superset:/tmp/ensure_ai_prompt_permissions.py
   docker compose exec -T superset sh -lc \
     'cd /app && PYTHONPATH=/app /app/.venv/bin/python /tmp/ensure_ai_prompt_permissions.py'
   ```

## Rollback

Rolling back code without rolling back the schema leaves the old code reading a
new shape — which usually "works" until it silently reads a column with a
different meaning.

1. **Stop the workers first**, so nothing writes while the schema moves:

   ```bash
   docker compose stop xp-prompt-worker xp-prompt-beat
   ```

2. **Downgrade one revision at a time**, checking the service between steps:

   ```bash
   docker compose exec -T xp-prompt-api sh -lc \
     'cd /app/xp_prompt && /app/.venv/bin/python -m alembic downgrade -1'
   ```

   Every migration ships a `downgrade()` — `tools/audit_schema.py` fails the build
   if one is missing or if it does not drop what the upgrade created.

3. **Redeploy the previous revision**, restart the runtime, and re-run the verify
   step above.

4. **If a migration cannot be downgraded safely** (a destructive change already
   ran), restore instead — see `restore.md`. Do not hand-edit the schema to
   avoid a restore: the backup is the known-good state, and a hand-edit is not.

## What "ready" means

`/health/ready` answers for the three dependencies (Postgres, Redis, Qdrant) and
returns `ready` only when all three are. `postgres: unavailable` after a
migration means `SCHEMA_REVISION` and the applied revision disagree — fix that
before looking anywhere else.
