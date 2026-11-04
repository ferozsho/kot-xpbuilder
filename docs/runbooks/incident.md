# Incident runbooks (P11-F12)

Each section is a symptom an on-call engineer actually sees, what it means, and
the commands that resolve or contain it. Start with the two readings that answer
most questions:

```bash
docker compose exec -T xp-prompt-api sh -lc 'curl -s http://127.0.0.1:8090/health/ready'
docker compose exec -T xp-prompt-api sh -lc 'curl -s http://127.0.0.1:8090/metrics'
```

## Provider outage (`http_402`, `http_429`, `no_provider`)

**See:** chat answers `403`/`502`/`503`, or the settings page's provider test
fails; `xp_prompt_provider_attempts{kind="failed"}` climbs.

**Mean:** the provider refused (billing, quota) or is unreachable. The breaker in
`ai_provider.circuit_open_until` deliberately skips a provider that keeps failing,
with an exponential back-off whose base is `provider.breaker_cooldown_seconds`.

```bash
# Is a breaker open, and until when?
docker compose exec -T xp-prompt-db psql -U xprompt -d xprompt -c \
 "SELECT name, enabled, consecutive_failures, circuit_open_until, last_error_code
    FROM ai_provider ORDER BY name"
```

* Wrong key or exhausted credit → fix it in *Settings → AI Provider*, then
  **Test**. The breaker closes on the first success; no restart is needed.
* One provider down: enable fallback (`provider.fallback_enabled`) and name a
  second provider (`provider.fallback`) — the save is refused with
  `invalid_routing` if that provider is not enabled.
* Everything down: chat answers `no_provider` with 503. That is the honest
  answer; do not bypass the guard.

## Usage queue backlog (`xp_prompt_outbox_events{kind="pending"}`)

**See:** the pending count grows instead of draining.

**Mean:** the worker is down, or delivery keeps failing. The ledger is durable —
the outbox exists so a failed *delivery* never loses an *accounted* request.

```bash
docker compose exec -T xp-prompt-api sh -lc \
 'curl -s -H "X-XP-Prompt-Token: $TOKEN" http://127.0.0.1:8090/ai/usage/outbox'
# Apply what is waiting, now:
docker compose exec -T xp-prompt-api sh -lc \
 'curl -s -X POST -H "X-XP-Prompt-Token: $TOKEN" -H "Content-Type: application/json" \
  -d "{\"limit\":200}" http://127.0.0.1:8090/ai/usage/outbox/drain'
```

Dead letters carry their own error. Fix the cause, then requeue one:
`POST /ai/usage/outbox/<event_id>/requeue`. Do not delete dead letters to make the
number go away.

## Stuck job

**See:** a job stays `running`, or `xp_prompt_jobs{kind="lease_expired"}` is
non-zero.

**Mean:** the worker that owned it died. The lease is the recovery mechanism:
`xp-prompt-recover-jobs` sweeps every minute and requeues the job, with a new
worker heartbeating its own lease between steps.

```bash
docker compose exec -T xp-prompt-db psql -U xprompt -d xprompt -c \
 "SELECT id, job_type, status, attempt_count, max_attempts, lease_owner, lease_expires_at
    FROM ai_job WHERE status = 'running' ORDER BY created_at DESC LIMIT 10"
# Force the sweep now:
docker compose exec -T xp-prompt-worker \
  celery -A xp_prompt.celery_app:app call xp_prompt.recover_jobs
```

A job that exhausts `max_attempts` lands in `failed` with an `error_code` — read
the code before retrying; a retry storm is what `MAX_TOTAL_ATTEMPTS` exists to
prevent.

## Bad repair pattern

**See:** a wrong statement keeps being reused, or `ai_repair_pattern` promoted
something dubious.

**Mean:** a correction was promoted on thin evidence.

```bash
docker compose exec -T xp-prompt-db psql -U xprompt -d xprompt -c \
 "SELECT id, failure_fingerprint, state, successes, failures, confidence FROM ai_repair_pattern
    WHERE state = 'promoted' ORDER BY updated_at DESC LIMIT 10"
```

Quarantine or deprecate it through
`POST /ai/sql/patterns/<pattern_id>/<action>` — the state machine refuses to
resurrect a disabled pattern on its own. Then set `repair.require_review` so the
loop holds promotion for a human, and raise `repair.min_confidence_percent`.

## Cost anomaly

**See:** spend ramping, `xp_prompt_budgets{kind="warning"}` rising.

```bash
docker compose exec -T xp-prompt-api sh -lc \
 'curl -s -H "X-XP-Prompt-Token: $TOKEN" "http://127.0.0.1:8090/ai/usage/summary?group_by=module,model"'
docker compose exec -T xp-prompt-api sh -lc \
 'curl -s -H "X-XP-Prompt-Token: $TOKEN" http://127.0.0.1:8090/ai/usage/alerts'
```

* Unpriced attempts (`unpriced` in the reconciliation report) mean a price is
  missing, not that the spend is zero: publish one at `POST /ai/usage/prices`.
* Set a budget (`POST /ai/usage/budgets`) with `hard_stop: true`; the guard
  reserves headroom before the call and answers 429 with the reason at the
  boundary.
* Breaches are delivered to `usage.alert_webhook_url`; `delivered: false` with
  `not_configured` means nobody is being told.

## Database timeout or saturation

**See:** SQL previews return `400` "The query could not be executed"; durations
climb.

`schema.sql.timeout_ms` is applied as a PostgreSQL `statement_timeout` per call,
and `sql.max_concurrent` is a Redis-backed slot counter — a saturated deployment
answers `429 sql_concurrency_exceeded` rather than queueing.

```bash
docker compose exec -T xp-prompt-api sh -lc 'curl -s http://127.0.0.1:8090/metrics | grep -E "slots|max_rows"'
docker compose exec -T xp-prompt-db psql -U xprompt -d xprompt -c \
 "SELECT pid, state, wait_event_type, now() - query_start AS age, left(query, 60)
    FROM pg_stat_activity WHERE datname = 'xprompt' ORDER BY age DESC LIMIT 10"
```

Lower `sql.timeout_ms` or `sql.max_rows` to shed load; widening them is how a
saturation becomes an outage. The ceilings cannot be raised past the platform
maximum, by design.

## Restore and rollback

See `restore.md` and `release.md`. Two rules: **verify a backup before you need
it**, and never hand-edit a schema to avoid a restore.
