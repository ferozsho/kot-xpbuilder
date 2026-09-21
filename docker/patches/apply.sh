#!/bin/sh
# Applies repository patches (docker/patches/*.patch) to the vendored Apache
# Superset source during the XPBuilder image build. Runs inside the
# `superset-src` build stage (Alpine, GNU patch installed via apk).
#
# Idempotent: a patch that is already applied is skipped; a patch that fails
# to apply against a pristine tree aborts the build.
set -eu

src="${1:-/src}"
patches="${2:-/patches}"

cd "$src"
count=0
for patch_file in "$patches"/*.patch; do
    [ -e "$patch_file" ] || continue
    base="$(basename "$patch_file")"
    if patch -p1 -N --batch --dry-run < "$patch_file" >/dev/null 2>&1; then
        echo "XPBuilder: applying patch $base"
        patch -p1 -N --batch < "$patch_file"
        count=$((count + 1))
    elif patch -p1 -N --batch --dry-run -R < "$patch_file" >/dev/null 2>&1; then
        echo "XPBuilder: patch already applied, skipping $base"
    else
        echo "XPBuilder: FAILED to apply patch $base (does it apply cleanly to a pristine 6.1.0 tree?)" >&2
        exit 1
    fi
done
echo "XPBuilder: applied $count patch(es)"
