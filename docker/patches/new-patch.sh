#!/usr/bin/env bash
#
# Export edits made in the vendored Apache Superset source (superset/) as a
# repository patch, and verify it applies cleanly to the pristine 6.1.0 tree.
#
# The vendored superset/ tree has no .git of its own — the pristine 6.1.0
# baseline lives in a separate repository (default
# ~/.cache/xpbuilder/superset-baseline.git) whose core.worktree points at
# superset/. This keeps the outer xpbuilder repo free of a nested repository.
# Make your edits inside superset/, then run this script to export them.
#
# Usage:
#   docker/patches/new-patch.sh <patch-name>
#       Export current superset/ edits to docker/patches/<patch-name>.patch
#       and verify the result applies cleanly to the pristine baseline.
#   docker/patches/new-patch.sh --verify [patch ...]
#       Dry-run apply one or more patches (default: every *.patch in
#       docker/patches/) against the pristine baseline.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
src="$root/superset"
patches_dir="$root/docker/patches"
baseline="${XPBUILDER_SUPERSET_GIT:-$HOME/.cache/xpbuilder/superset-baseline.git}"

git_superset() {
    git --git-dir="$baseline" --work-tree="$src" "$@"
}

require_baseline() {
    if [ ! -d "$baseline" ]; then
        echo "ERROR: Superset baseline repository not found at $baseline." >&2
        echo "  Initialize it once with:" >&2
        echo "    mkdir -p \"$HOME/.cache/xpbuilder\"" >&2
        echo "    git init \"$baseline\"" >&2
        echo "    git --git-dir=\"$baseline\" config core.worktree \"$src\"" >&2
        echo "    git --git-dir=\"$baseline\" --work-tree=\"$src\" add -A" >&2
        echo "    git --git-dir=\"$baseline\" --work-tree=\"$src\" commit -m 'apache/superset 6.1.0 baseline'" >&2
        exit 1
    fi
    if ! git --git-dir="$baseline" rev-parse --verify --quiet HEAD >/dev/null; then
        echo "ERROR: Superset baseline repository has no baseline commit." >&2
        exit 1
    fi
}

# Verify a patch applies to the pristine baseline (a git worktree at HEAD).
verify_patch() {
    local patch_file="$1"
    local pristine="$2"
    if (cd "$pristine" && patch -p1 -N --batch --dry-run < "$patch_file" >/dev/null 2>&1); then
        echo "OK    $(basename "$patch_file")"
        return 0
    fi
    if (cd "$pristine" && patch -p1 -N --batch --dry-run -R < "$patch_file" >/dev/null 2>&1); then
        echo "SKIP  $(basename "$patch_file") (already applied)"
        return 0
    fi
    echo "FAIL  $(basename "$patch_file")" >&2
    return 1
}

# Extract the pristine baseline tree to a temp dir (avoids worktree + core.worktree conflicts).
pristine_dir() {
    local dir
    dir="$(mktemp -d)"
    git --git-dir="$baseline" archive HEAD | tar -x -C "$dir"
    printf '%s' "$dir"
}

# --- verify mode -----------------------------------------------------------
if [ "${1:-}" = "--verify" ]; then
    shift
    require_baseline
    files=("$@")
    if [ "${#files[@]}" -eq 0 ]; then
        mapfile -t files < <(find "$patches_dir" -maxdepth 1 -name '*.patch' | sort)
    fi
    if [ "${#files[@]}" -eq 0 ]; then
        echo "No patches to verify in $patches_dir"
        exit 0
    fi
    pristine="$(pristine_dir)"
    trap 'rm -rf "$pristine"' EXIT
    ok=1
    for patch_file in "${files[@]}"; do
        if [ ! -e "$patch_file" ]; then
            echo "FAIL  $patch_file (no such file)" >&2
            ok=0
            continue
        fi
        verify_patch "$patch_file" "$pristine" || ok=0
    done
    [ "$ok" -eq 1 ] || exit 1
    exit 0
fi

# --- generate mode ---------------------------------------------------------
name="${1:-}"
if [ -z "$name" ]; then
    echo "Usage: docker/patches/new-patch.sh <patch-name> | --verify [patch ...]" >&2
    exit 2
fi
case "$name" in
    *.patch) ;;
    *) name="${name}.patch" ;;
esac
out="$patches_dir/$name"

require_baseline

# Mark untracked files as intent-to-add so `git diff HEAD` includes new files.
git_superset add -N .

if git_superset diff --quiet HEAD; then
    echo "No changes in $src — nothing to export." >&2
    exit 1
fi

git_superset diff --text HEAD > "$out"
echo "Wrote $out ($(wc -l < "$out") lines)"

pristine="$(pristine_dir)"
trap 'rm -rf "$pristine"' EXIT
if verify_patch "$out" "$pristine"; then
    echo "Verified: $name applies cleanly to the pristine 6.1.0 baseline."
else
    echo "ERROR: $name does NOT apply cleanly to the pristine baseline." >&2
    echo "  Re-check the edit and rerun." >&2
    exit 1
fi
