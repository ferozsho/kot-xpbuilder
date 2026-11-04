# Backup and restore (P11-F08)

## What a backup contains

`bin/backup.sh <env file> <compose project> [directory]` writes one directory with
a manifest and one artifact per durable store:

| artifact | store | what it holds |
| --- | --- | --- |
| `superset-metadata.dump` | Superset PostgreSQL | dashboards, charts, datasets, users and ownership |
| `uploads.dump` | `xpbuilder_uploads` | uploaded CSV/Excel/columnar files |
| `mariadb.dump` | bundled MariaDB | the site data managed in phpMyAdmin |
| `ai.dump` | `xprompt` | conversations, messages, jobs, usage ledger, budgets, billing imports, repair patterns, drafts, settings and their audit |
| `qdrant/<collection>.snapshot` | Qdrant | authorized embeddings with tenant and source metadata |
| `ai-row-counts.txt`, `manifest.json` | — | exact per-table counts and a sha256 per artifact |

Redis is **not** in the backup, and that is deliberate: it holds a statement cache
and a slot counter, both of which are safe to lose (P11-F05 evidence). There is
nothing durable in it to back up.

**Secrets are not in the backup.** Provider API keys are stored sealed with the
deployment's root secret; the dump contains the ciphertext, never the key. A
restore therefore needs the same `XP_PROMPT_SERVICE_SECRET` — without it the
provider rows restore but cannot be decrypted, and the settings page shows them as
broken rather than pretending they work. The manifest lists the secrets a restore
must supply:

```bash
python3 -c "import json;print(json.load(open('<backup>/manifest.json'))['secrets_not_included'])"
```

## Verifying a backup (do this before you need it)

```bash
bin/verify-backup.sh .env kot /var/backups/xpbuilder/<timestamp>
```

The drill restores each dump into a **scratch database** (`xprompt_verify`,
`superset_verify`), counts the rows, compares them with the counts recorded at
backup time, prints the Superset dashboard/chart/dataset/user totals, and drops
the scratch databases whatever happens. It fails on a checksum mismatch, a missing
artifact, a table that did not restore, or a count that drifted.

A real run reports every table, for example:

```
OK ai_conversation: 11 rows
OK ai_message: 18 rows
OK ai_provider_attempt: 4 rows
OK ai_setting_audit: 19 rows
  restored dashboards|5
  restored charts|41
  restored datasets|115
  restored users|3
Backup verified: every checksum and row count matches
```

## Restoring

```bash
# 1. Stop writers.
docker compose stop xp-prompt-worker xp-prompt-beat superset
# 2. Restore (the script demands an explicit confirmation string).
bin/restore.sh .env kot /var/backups/xpbuilder/<timestamp>/superset-metadata.dump restore-kot
# 3. The AI database is restored the same way it is dumped:
docker compose exec -T xp-prompt-db \
  pg_restore -U xprompt -d xprompt --clean --if-exists --no-owner < <timestamp>/ai.dump
# 4. Qdrant collections come back from their snapshots (per collection):
#    POST /collections/<name>/snapshots/upload  with the snapshot file
# 5. Start writers and verify.
docker compose up -d && bin/verify-backup.sh .env kot /var/backups/xpbuilder/<timestamp>
```

## Retention is not a backup

`retention.*` deletes old rows on a schedule; it does not replace a backup and a
backup does not change retention. Budgets, billing imports, the configuration
audit and the tenant row are excluded from every sweep — deleting the accounting
record to save space would destroy the evidence that answers "what did this cost
and who changed it".
