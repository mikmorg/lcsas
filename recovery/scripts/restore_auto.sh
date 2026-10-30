#!/bin/sh
# restore_auto.sh -- non-interactive variant of restore.sh.
#
# Reads password from $LCSAS_PWFILE (must be set).  Restores the latest
# snapshot of every repository discovered under $RECOVERY/repos/.
#
# Exit codes:
#   0 -- all repositories restored
#   1 -- at least one repository failed
#   2 -- usage error
set -eu

if [ $# -lt 2 ]; then
    printf 'usage: %s RECOVERY_ROOT TARGET_ROOT\n' "$0" >&2
    printf 'requires LCSAS_PWFILE environment variable to be set\n' >&2
    exit 2
fi
if [ -z "${LCSAS_PWFILE:-}" ] || [ ! -f "$LCSAS_PWFILE" ]; then
    printf 'LCSAS_PWFILE must point to an existing password file\n' >&2
    exit 2
fi

RECOVERY="$1"
TARGET="$2"
fail=0

# Discover repositories: each subdir of $RECOVERY/repos/ with a keys/ entry.
if [ -d "$RECOVERY/repos" ]; then
    repos_root="$RECOVERY/repos"
else
    repos_root="$RECOVERY"
fi

for r in "$repos_root"/*/; do
    [ -d "$r" ] || continue
    [ -d "$r/keys" ] || continue
    name="$(basename "$r")"
    out="$TARGET/$name"
    mkdir -p "$out"
    printf '==> restoring %s -> %s\n' "$name" "$out" >&2
    # Select THIS repository via LCSAS_REPO so restore.sh restores the
    # right one for each iteration instead of re-resolving on its own --
    # which, on a multi-repository archive, would hit the interactive
    # selection prompt (EOF → fail) or restore the same one every loop
    # (issue #373).
    rc=0
    LCSAS_REPO="$name" sh "$RECOVERY/scripts/restore.sh" \
             "$RECOVERY" "$out" latest || rc=$?
    if [ "$rc" -ne 0 ]; then
        if [ "$rc" -eq 77 ]; then
            # Wrong password for THIS repository (issue #384).  Each
            # repository has its own password, so one shared LCSAS_PWFILE
            # can be right for some and wrong for others -- keep looping,
            # but say exactly which repository rejected the password.
            printf '!!! restore of %s FAILED: wrong password (exit 77).\n' \
                   "$name" >&2
            printf '    LCSAS_PWFILE does not decrypt this repository'"'"'s keys;\n' >&2
            printf '    the remaining repositories are still attempted.\n' >&2
        else
            printf '!!! restore of %s FAILED (exit %s)\n' "$name" "$rc" >&2
        fi
        fail=1
    fi
done

exit "$fail"
