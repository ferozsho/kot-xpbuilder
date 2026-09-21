#!/usr/bin/env bash

set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

python3 -m py_compile config/superset_config.py docker/*.py tests/contract/*.py
python3 tests/contract/check_compatibility.py
python3 -m json.tool compatibility.json >/dev/null

for file in bin/*.sh bin/xpbuilder docker/*.sh docker/patches/*.sh ops/kot5 tests/*.sh; do
    bash -n "$file"
done

if command -v shellcheck >/dev/null 2>&1; then
    shellcheck bin/*.sh bin/xpbuilder docker/*.sh docker/patches/*.sh ops/kot5 tests/*.sh
fi

if rg -n \
    'CHANGE-ME-IN-PRODUCTION|guest-token-dev-secret|superset_ro_pass|postgresql://superset:superset|apache/superset:latest' \
    Dockerfile compose.yml config docker bin; then
    echo "ERROR: insecure or floating runtime default detected" >&2
    exit 1
fi

if git ls-files | grep -Eq '(^|/)\.env($|\.)'; then
    echo "ERROR: an env file is tracked by git" >&2
    exit 1
fi

temporary="$(mktemp -d)"
cleanup() {
    rm -rf "$temporary"
}
trap cleanup EXIT

bin/bootstrap-env.sh \
    --env-file "$temporary/.env" \
    --instance xpbuilderstatic \
    --host-port 18088 >/dev/null

bin/validate-env.sh "$temporary/.env" >/dev/null

# Secret hygiene: a group-accessible .env is rejected unless the site opts in
# (hosted client workspaces share it with a restricted group), while access by
# other users is never tolerated.
chmod 0660 "$temporary/.env"
if bin/validate-env.sh "$temporary/.env" >/dev/null 2>&1; then
    echo "ERROR: a group-accessible .env was accepted without --allow-group-env" >&2
    exit 1
fi
bin/validate-env.sh --allow-group-env "$temporary/.env" >/dev/null

chmod 0664 "$temporary/.env"
if bin/validate-env.sh --allow-group-env "$temporary/.env" >/dev/null 2>&1; then
    echo "ERROR: an .env readable by other users was accepted" >&2
    exit 1
fi
chmod 0600 "$temporary/.env"
docker compose \
    --env-file "$temporary/.env" \
    -f compose.yml \
    -p xpbuilderstatic \
    config --quiet

echo "XPBuilder static checks passed"
