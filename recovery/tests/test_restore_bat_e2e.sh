#!/bin/sh
# test_restore_bat_e2e.sh -- local wine smoke for recovery/scripts/restore.bat
# (UX-08 layer 2, local arm).
#
# The Windows recovery journey had NO executable test of any kind: the
# unit dispatcher test (tests/unit/test_restore_bat_dispatcher.py) only
# greps the .bat text, and recovery/tests/test_e2e_windows.sh drives the
# lcsas-restore.exe directly -- it never runs the .bat itself.  This is a
# smoke gate that actually interprets the .bat under `wine cmd /c`.
#
# SCOPE / faithfulness caveat: wine's cmd.exe is NOT a faithful CMD
# interpreter (multi-line echo, delayed expansion, and drive-letter
# mapping all differ).  So this asserts ONLY the deterministic, early
# control-flow that wine reproduces reliably:
#
#   1. no-repo  -> exits non-zero AND prints the "could not find an LCSAS
#                  backup set" error (proves the .bat parses, runs, and
#                  the guard fires).
#   2. holographic -> with a metadata\<tenant>\ repo (the layout the meta
#                  builder actually writes; NO repo\ dir) and LCSAS_REPO
#                  set, the .bat gets PAST repo discovery and prints the
#                  resolved Repo: line + canonical target triple (proves
#                  the UX-01 holographic-layout discovery reaches the
#                  interactive prompt / tier-1 dispatch).
#
# A byte-correct end-to-end restore through the .bat is owned by the
# INFRA-01 windows-latest CI job (real CMD + a real lcsas-restore.exe);
# this local script is the cheap pre-flight that catches gross .bat
# breakage without GitHub.
#
# Exit 0 = pass; 77 = skip (no wine); non-zero = fail.

set -eu

HERE="$(cd "$(dirname "$0")" && pwd)"
BAT="$HERE/../scripts/restore.bat"

if [ ! -f "$BAT" ]; then
    echo "FAIL: restore.bat not found at $BAT" >&2
    exit 1
fi

if ! command -v wine >/dev/null 2>&1; then
    echo "SKIP: wine not installed; restore.bat smoke needs wine cmd." >&2
    exit 77
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/lcsas-bat-smoke.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Mirror the on-disc layout so %~dp0..\ resolves the way it does on a
# real meta disc: <root>/recovery/scripts/restore.bat.
mkdir -p "$WORK/recovery/scripts" "$WORK/recovery/bin"
cp "$BAT" "$WORK/recovery/scripts/restore.bat"

# LCSAS_NO_RELOCATE=1 keeps the single-drive RAM relocation out of the
# smoke (it depends on %TEMP% writability under wine and is not what we
# are testing).  LCSAS_TARGET pins the triple so the run does not depend
# on wine's reported PROCESSOR_ARCHITECTURE.
run_bat() {
    # $1 = stdin file.  Echo back the exit code on its own line so the
    # caller can assert on it without $? surviving the pipe.
    ( cd "$WORK" \
        && WINEDEBUG=-all LCSAS_NO_RELOCATE=1 \
           LCSAS_TARGET=x86_64-pc-windows-gnu \
           timeout 120 wine cmd /c "recovery\\scripts\\restore.bat < $1" ) \
        2>&1
}

fail=0

# ---- Case 1: no repo present -> guarded failure --------------------
: > "$WORK/empty.txt"
out1="$(run_bat empty.txt || true)"
rc1=0
( cd "$WORK" \
    && WINEDEBUG=-all LCSAS_NO_RELOCATE=1 LCSAS_TARGET=x86_64-pc-windows-gnu \
       timeout 120 wine cmd /c "recovery\\scripts\\restore.bat < empty.txt" \
       >/dev/null 2>&1 ) || rc1=$?

if [ "$rc1" -eq 0 ]; then
    echo "FAIL[no-repo]: expected non-zero exit, got 0" >&2
    fail=1
fi
if ! printf '%s' "$out1" | grep -q "could not find an LCSAS backup set"; then
    echo "FAIL[no-repo]: missing 'could not find an LCSAS backup set' error:" >&2
    printf '%s\n' "$out1" >&2
    fail=1
fi
[ "$fail" -eq 0 ] && echo "PASS[no-repo]: exit $rc1, backup-set guard fired"

# ---- Case 2: holographic metadata\<tenant>\ layout (UX-01) ---------
# Build the layout the meta builder actually writes: the repo material
# lives under <disc-root>/metadata/<tenant>/, with NO recovery/repo dir.
# %RECOVERY% resolves to <root>/recovery (recovery/bin exists), so the
# .bat must climb to <root>/metadata/<tenant>/.  LCSAS_REPO selects it.
mkdir -p "$WORK/metadata/alpha/keys" "$WORK/metadata/alpha/index"
# Feed an unwritable target + a password so the run advances PAST repo
# discovery and the arch banner without needing a real binary.
printf 'Q:\\nonexistent\\out\npw\n' > "$WORK/in2.txt"
out2="$( ( cd "$WORK" \
    && WINEDEBUG=-all LCSAS_NO_RELOCATE=1 \
       LCSAS_TARGET=x86_64-pc-windows-gnu LCSAS_REPO=alpha \
       timeout 120 wine cmd /c "recovery\\scripts\\restore.bat < in2.txt" ) \
    2>&1 || true )"

if ! printf '%s' "$out2" | grep -q "x86_64-pc-windows-gnu"; then
    echo "FAIL[holographic]: canonical target triple not printed:" >&2
    printf '%s\n' "$out2" >&2
    fail=1
fi
if ! printf '%s' "$out2" | grep -q "Repo:"; then
    echo "FAIL[holographic]: repo discovery banner ('Repo:') not reached:" >&2
    printf '%s\n' "$out2" >&2
    fail=1
fi
if ! printf '%s' "$out2" | grep -q "alpha"; then
    echo "FAIL[holographic]: selected tenant 'alpha' not in Repo: line:" >&2
    printf '%s\n' "$out2" >&2
    fail=1
fi
[ "$fail" -eq 0 ] && echo "PASS[holographic]: metadata\\<tenant>\\ discovery reached the prompt"

if [ "$fail" -ne 0 ]; then
    echo "restore.bat smoke: FAIL" >&2
    exit 1
fi
echo "restore.bat smoke: OK"
exit 0
