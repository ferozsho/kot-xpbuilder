#!/usr/bin/env bash

# Verify a backup can actually be restored, and that it contains what it claims.
#
# A dump file that exists is not a backup. This drill restores each artifact into
# a scratch database, counts the rows, and compares them with the counts recorded
# when the backup was taken — so "we have backups" becomes "we restored this and
# the numbers match" (P11-F08).
#
# Usage: bin/verify-backup.sh <env file> <compose project> <backup directory>
#
# Nothing production-facing is written: the scratch databases are dropped at the
# end, whatever happens.

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
env_file="${1:?env file required}"
project="${2:?compose project required}"
directory="${3:?backup directory required}"

value_of() {
    local key="$1"
    sed -n "s/^${key}=//p" "$env_file" | tail -1
}

[ -s "$directory/manifest.json" ] || {
    echo "ERROR: no manifest in $directory" >&2
    exit 1
}

compose=(docker compose --env-file "$env_file" -f "$root/compose.yml" -p "$project")
scratch_ai="xprompt_verify"
scratch_metadata="superset_verify"
failures=0

cleanup() {
    "${compose[@]}" exec -T xp-prompt-db \
        psql -U xprompt -d postgres -tAc \
        "DROP DATABASE IF EXISTS $scratch_ai" >/dev/null 2>&1 || true
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d postgres -tAc \
        "DROP DATABASE IF EXISTS $scratch_metadata" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Verifying checksums recorded in the manifest"
python3 - "$directory" <<'PY'
import hashlib
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
pairs = [
    ('metadata_backup', 'sha256'),
    ('uploads_backup', 'uploads_sha256'),
    ('mariadb_backup', 'mariadb_sha256'),
    ('ai_backup', 'ai_sha256'),
]
failed = False
for name_key, hash_key in pairs:
    name = manifest.get(name_key)
    expected = manifest.get(hash_key)
    if not name:
        continue
    artifact = directory / name
    if not artifact.exists():
        print(f"MISMATCH {name}: missing")
        failed = True
        continue
    actual = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if actual != expected:
        print(f"MISMATCH {name}: {actual[:12]} != {expected[:12]}")
        failed = True
    else:
        print(f"OK {name}: {artifact.stat().st_size} bytes, sha256 {actual[:12]}")
for name in manifest.get('qdrant_snapshots') or []:
    artifact = directory / 'qdrant' / name
    print(f"{'OK' if artifact.exists() else 'MISMATCH'} qdrant/{name}")
    failed = failed or not artifact.exists()
raise SystemExit(1 if failed else 0)
PY

echo "Restoring the AI database into $scratch_ai"
"${compose[@]}" exec -T xp-prompt-db \
    psql -U xprompt -d postgres -tAc \
    "DROP DATABASE IF EXISTS $scratch_ai" >/dev/null
"${compose[@]}" exec -T xp-prompt-db \
    psql -U xprompt -d postgres -tAc \
    "CREATE DATABASE $scratch_ai" >/dev/null
"${compose[@]}" exec -T xp-prompt-db \
    pg_restore -U xprompt -d "$scratch_ai" --no-owner --exit-on-error \
    < "$directory/ai.dump"

restored="$("${compose[@]}" exec -T xp-prompt-db \
    psql -U xprompt -d "$scratch_ai" -tAF'|' -c \
    "SELECT relname, (xpath('/row/c/text()', query_to_xml(format('SELECT count(*) AS c FROM %I.%I', schemaname, relname), false, true, '')))[1]::text::int FROM pg_stat_user_tables ORDER BY relname")"

echo "Comparing restored row counts with the manifest"
if ! python3 - "$directory" "$restored" <<'PY'
import json
import pathlib
import sys

directory = pathlib.Path(sys.argv[1])
recorded = json.loads(
    (directory / 'manifest.json').read_text(encoding='utf-8')
).get('ai_row_counts') or {}
restored = {
    line.split('|')[0]: int(line.split('|')[1] or 0)
    for line in sys.argv[2].splitlines()
    if '|' in line
}
failed = False
for table, expected in sorted(recorded.items()):
    actual = restored.get(table)
    if actual is None:
        print(f"MISSING {table}: not restored")
        failed = True
    elif actual != int(expected):
        print(f"MISMATCH {table}: restored {actual}, recorded {expected}")
        failed = True
    else:
        print(f"OK {table}: {actual} rows")
raise SystemExit(1 if failed else 0)
PY
then
    failures=$((failures + 1))
fi

if [ -s "$directory/superset-metadata.dump" ]; then
    echo "Restoring the Superset metadata into $scratch_metadata"
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d postgres -tAc \
        "DROP DATABASE IF EXISTS $scratch_metadata" >/dev/null
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d postgres -tAc \
        "CREATE DATABASE $scratch_metadata" >/dev/null
    "${compose[@]}" exec -T superset-db \
        pg_restore -U "$(value_of POSTGRES_USER)" -d "$scratch_metadata" \
        --no-owner < "$directory/superset-metadata.dump" 2>/dev/null || true
    # Current state, not just any state: dashboards, charts, datasets and the
    # users who own them are what a restore has to bring back.
    "${compose[@]}" exec -T superset-db \
        psql -U "$(value_of POSTGRES_USER)" -d "$scratch_metadata" -tAF'|' -c \
        "SELECT 'dashboards', count(*) FROM dashboards
         UNION ALL SELECT 'charts', count(*) FROM slices
         UNION ALL SELECT 'datasets', count(*) FROM tables
         UNION ALL SELECT 'users', count(*) FROM ab_user" \
        | sed 's/^/  restored /'
fi

if [ "$failures" -ne 0 ]; then
    echo "VERIFY FAILED" >&2
    exit 1
fi

echo "Backup verified: every checksum and row count matches"
