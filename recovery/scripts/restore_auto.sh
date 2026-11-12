#!/bin/sh
# restore_auto.sh -- non-interactive variant of restore.sh.
#
# Reads password from $LCSAS_PWFILE (must be set).  Restores the latest
# snapshot of every backup set (repository) it finds, each into its own
# folder TARGET_ROOT/<id>, by running restore.sh once per set.  This is
# the meta disc's one automation script (#689); on the disc run it as
#   LCSAS_PWFILE=/full/path/pw sh /mnt/recovery/scripts/restore_auto.sh \
#       /mnt/recovery ~/restored
#
# Where it looks for the sets (each a folder holding keys/):
#   $RECOVERY/repos/*/           when $RECOVERY/repos/ exists; otherwise
#   $RECOVERY/*/, $RECOVERY/metadata/*/ and $RECOVERY/../metadata/*/
#   (the last is the meta disc's metadata/ folder, next to recovery/).
#
# Exit codes:
#   0 -- all repositories restored
#   1 -- at least one repository failed, or no repository was found
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

# restore.sh's own questions are for a person at a terminal: the newest
# data disc first, and "restore another backup set?" after each one.
# This loop restores every set itself and may run on a terminal, so it
# turns both off (#605, #609).
LCSAS_NEWEST_DISC_PROMPT=0
LCSAS_ANOTHER_SET=0
export LCSAS_NEWEST_DISC_PROMPT LCSAS_ANOTHER_SET

# Discover the sets: each folder with a keys/ folder, once per id (the
# meta disc and a hand-made tree can both hold a copy of one set).
if [ -d "$RECOVERY/repos" ]; then
    set -- "$RECOVERY/repos"
else
    set -- "$RECOVERY" "$RECOVERY/metadata" "$RECOVERY/../metadata"
fi
seen="/"
found=0

for root in "$@"; do
for r in "$root"/*/; do
    [ -d "$r" ] || continue
    [ -d "$r/keys" ] || continue
    r="${r%/}"
    case "$seen" in *"/${r##*/}/"*) continue ;; esac
    seen="$seen${r##*/}/"
    found=$((found + 1))
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
done

if [ "$found" -eq 0 ]; then
    printf '!!! no backup set found: no folder with keys/ under\n' >&2
    printf '    %s/repos, %s, %s/metadata or %s/../metadata.\n' \
           "$RECOVERY" "$RECOVERY" "$RECOVERY" "$RECOVERY" >&2
    printf '    Nothing was restored.  Run restore.sh itself with a data disc\n' >&2
    printf '    mounted, and LCSAS_REPO=<id> to choose the set.\n' >&2
    exit 1
fi

exit "$fail"
