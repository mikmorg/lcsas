#!/bin/sh
# test_renamed_names.sh -- the tier-1 programs refuse their retired names.
#
# #934 / ADR 0005: interface names follow the glossary, renamed outright
# with no aliases.  An old flag or a set old environment variable fails
# with exit 2 (usage) and one line naming the replacement:
#   lcsas-restore --target            -> use --restore-folder
#   lcsas-shares  LCSAS_KEYSHARE_PASSPHRASE -> use LCSAS_SHARE_PASSPHRASE
#
# Usage: sh tests/test_renamed_names.sh [BUILD_DIR]   (default: build)
# `make test` runs it against the binaries it just built, so coverage-c
# and sanitize reach these branches too.

set -u

BUILD=${1:-build}
RESTORE="$BUILD/lcsas-restore"
SHARES="$BUILD/lcsas-shares"
fails=0
WORK=$(mktemp -d "${TMPDIR:-/tmp}/lcsas_renamed.XXXXXX") || exit 1
trap 'rm -rf "$WORK"' EXIT INT TERM

# check NAME WANT_RC WANT_ERR CMD... : run CMD, compare exit status and
# the whole of stderr.
check() {
    name=$1; want_rc=$2; want_err=$3; shift 3
    "$@" >"$WORK/out" 2>"$WORK/err" </dev/null
    rc=$?
    got_err=$(cat "$WORK/err")
    if [ "$rc" -ne "$want_rc" ]; then
        printf 'FAIL %s: exit %s, want %s\n' "$name" "$rc" "$want_rc"
        printf '  stderr: %s\n' "$got_err"
        fails=$((fails + 1))
    elif [ "$got_err" != "$want_err" ]; then
        printf 'FAIL %s: stderr %s\n  want   %s\n' "$name" "$got_err" "$want_err"
        fails=$((fails + 1))
    elif [ -s "$WORK/out" ]; then
        printf 'FAIL %s: wrote to stdout\n' "$name"
        fails=$((fails + 1))
    else
        printf 'ok   %s\n' "$name"
    fi
}

for b in "$RESTORE" "$SHARES"; do
    if [ ! -x "$b" ]; then
        printf 'FAIL: %s is not built\n' "$b"
        exit 1
    fi
done

# ── lcsas-restore: --target ──────────────────────────────────────
TARGET_MSG="lcsas-restore: --target was renamed: use --restore-folder"
check "lcsas-restore --target (with the other flags)" 2 "$TARGET_MSG" \
    "$RESTORE" --repo "$WORK/repo" --password-file "$WORK/pw" \
    --target "$WORK/out_dir"
check "lcsas-restore --target first" 2 "$TARGET_MSG" \
    "$RESTORE" --target "$WORK/out_dir"
# Refused before any work: no folder was made.
if [ -e "$WORK/out_dir" ]; then
    printf 'FAIL lcsas-restore --target created the folder\n'
    fails=$((fails + 1))
fi
# The new flag is known: without --repo it is a plain usage error that
# names --restore-folder, not "unknown argument".
"$RESTORE" --restore-folder "$WORK/out_dir" >/dev/null 2>"$WORK/err" </dev/null
rc=$?
if [ "$rc" -ne 2 ] || grep -q 'unknown argument' "$WORK/err" \
        || ! grep -q -- '--restore-folder DIR' "$WORK/err" \
        || grep -q -- '--target' "$WORK/err"; then
    printf 'FAIL lcsas-restore --restore-folder: exit %s, stderr:\n' "$rc"
    cat "$WORK/err"
    fails=$((fails + 1))
else
    printf 'ok   lcsas-restore --restore-folder is known; usage names it\n'
fi

# ── lcsas-shares: LCSAS_KEYSHARE_PASSPHRASE ──────────────────────
PP_MSG="lcsas-shares: LCSAS_KEYSHARE_PASSPHRASE was renamed: use LCSAS_SHARE_PASSPHRASE"
check "lcsas-shares with the old variable set" 2 "$PP_MSG" \
    env LCSAS_KEYSHARE_PASSPHRASE=x "$SHARES" "$WORK/none.txt"
# Set but empty is still set: refused.
check "lcsas-shares with the old variable set empty" 2 "$PP_MSG" \
    env LCSAS_KEYSHARE_PASSPHRASE= "$SHARES"
# Refused first, before --help.
check "lcsas-shares --help with the old variable set" 2 "$PP_MSG" \
    env LCSAS_KEYSHARE_PASSPHRASE=x "$SHARES" --help
# Without it, --help works and names the program lcsas-shares.
env -u LCSAS_KEYSHARE_PASSPHRASE "$SHARES" --help >"$WORK/out" 2>&1
rc=$?
if [ "$rc" -ne 0 ] || ! grep -q '^usage: lcsas-shares ' "$WORK/out" \
        || grep -q 'keyshare' "$WORK/out"; then
    printf 'FAIL lcsas-shares --help: exit %s, output:\n' "$rc"
    cat "$WORK/out"
    fails=$((fails + 1))
else
    printf 'ok   lcsas-shares --help names lcsas-shares\n'
fi

if [ "$fails" -ne 0 ]; then
    printf 'test_renamed_names: %d FAILED\n' "$fails"
    exit 1
fi
printf 'test_renamed_names: OK\n'
