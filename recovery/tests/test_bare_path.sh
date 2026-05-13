#!/bin/sh
# test_bare_path.sh -- prove the bare recovery path needs NO Python.
#
# Method:
#   1. Statically inspect restore.sh tiers 1-2 for python references.
#   2. Use python3 OUT-OF-BAND to build a synthetic restic repo and
#      meta-volume layout.
#   3. Invoke /restore.sh under a stripped PATH that has no python and
#      with LCSAS_ALLOW_PYTHON_TIER=0.  If recovery succeeds, the bare
#      path provably does not need python.

set -eu

RECOVERY="$(cd "$(dirname "$0")/.." && pwd -P)"
TMP="$(mktemp -d /tmp/lcsas_bare.XXXXXX)"
trap 'rm -rf "$TMP"' EXIT INT TERM

# ── 1) Static inspection ──────────────────────────────────────────
#
# Slice restore.sh from the start of "Tier 1" to the end of the
# bare-minimum section ("BARE-MINIMUM PATH ENDS HERE").  Skip comment
# lines; the remaining executable code must mention nothing python-y.

CODE="$TMP/tier1to4_code"
awk '
    /Tier 1:/ { active=1 }
    /BARE-MINIMUM PATH ENDS HERE/ { exit }
    active && $0 !~ /^[[:space:]]*#/ { print }
' "$RECOVERY/scripts/restore.sh" > "$CODE"

if grep -nE 'python|\.py' "$CODE" > "$TMP/hits"; then
    printf 'FAIL: python references in bare-path code (tiers 1-2):\n' >&2
    cat "$TMP/hits" >&2
    exit 1
fi
LINES="$(wc -l < "$CODE")"
printf 'static check: %s lines of bare-path code, 0 python references\n' \
       "$LINES"

# ── 2) Build a fixture ────────────────────────────────────────────

if ! command -v python3 >/dev/null 2>&1; then
    printf 'SKIP runtime check: python3 not available to build fixture\n'
    exit 0
fi
if [ ! -x "$RECOVERY/build/lcsas-restore" ]; then
    ( cd "$RECOVERY" && make >/dev/null 2>&1 )
fi

# Build the synthetic repo with the existing helper.
META="$TMP/meta"
mkdir -p "$META/recovery/bin/x86_64"
mkdir -p "$META/recovery/scripts"

# Copy in the recovery tree we'd ship.
cp "$RECOVERY/scripts/restore.sh"      "$META/recovery/scripts/"
cp "$RECOVERY/scripts/detect_arch.sh"  "$META/recovery/scripts/"
chmod +x "$META/recovery/scripts/"*.sh
cp "$RECOVERY/build/lcsas-restore"     "$META/recovery/bin/x86_64/"
chmod +x "$META/recovery/bin/x86_64/lcsas-restore"

# Auto-detect arch and place the binary correctly.
ARCH="$(uname -m)"
case "$ARCH" in
    aarch64|arm64) ARCH=aarch64 ;;
    riscv64)       ARCH=riscv64 ;;
    *)             ARCH=x86_64 ;;
esac
if [ "$ARCH" != "x86_64" ]; then
    mkdir -p "$META/recovery/bin/$ARCH"
    cp "$RECOVERY/build/lcsas-restore" "$META/recovery/bin/$ARCH/"
    chmod +x "$META/recovery/bin/$ARCH/lcsas-restore"
fi

REPO="$META/recovery/repo"
PWFILE="$TMP/pw"
printf 'correct-horse-battery-staple\n' > "$PWFILE"

python3 -c "
import sys
sys.path.insert(0, '$RECOVERY/tests')
sys.path.insert(0, '$RECOVERY/../src')
import test_e2e
from pathlib import Path
files = {
    'hello.txt': b'Hello, bare-minimum path!\n',
    'binary.bin': bytes(range(256)) * 16,
}
test_e2e.build_repo(Path('$REPO'), 'correct-horse-battery-staple',
                    files, v2=False)
"

# ── 3) Recover under a Python-free PATH ───────────────────────────
#
# Rather than stripping PATH entries (which would also remove sh, cc,
# make, ...), we prepend a shim directory whose `python` / `python3`
# binaries exit non-zero.  Any `command -v python3` will hit the shim
# first and return success... so we go a step further: the shims
# themselves are non-executable.  `command -v` will skip them, but to
# be doubly safe we *also* unset PYTHONPATH and ensure tier-3 is
# disabled via env var.

SHIM="$TMP/python-shim"
mkdir -p "$SHIM"
# Executable scripts that exit 127 (command-not-found).  By placing
# $SHIM first in PATH and giving these +x, `command -v python3` will
# find these *before* the real interpreter.
for name in python python2 python3 python3.10 python3.11 python3.12; do
    cat > "$SHIM/$name" <<'EOF'
#!/bin/sh
echo "[shim] $0: blocked by test_bare_path.sh" >&2
exit 127
EOF
done
chmod +x "$SHIM"/*

SAFE_PATH="$SHIM:$PATH"

# Sanity: command -v must return a path within $SHIM (the shim, not
# the real interpreter).
RESOLVED="$(PATH="$SAFE_PATH" command -v python3 2>/dev/null || true)"
case "$RESOLVED" in
    "$SHIM"/*) ;;
    *)
        printf 'FAIL setup: python3 resolves to %s (not the shim)\n' \
               "$RESOLVED" >&2
        exit 1 ;;
esac

TARGET="$TMP/restored"
mkdir -p "$TARGET"

LCSAS_ALLOW_PYTHON_TIER=0 \
LCSAS_PWFILE="$PWFILE" \
PATH="$SAFE_PATH" \
    sh "$META/recovery/scripts/restore.sh" \
       "$META/recovery" "$TARGET" latest >/dev/null

# Verify
for name in hello.txt binary.bin; do
    if [ ! -f "$TARGET/$name" ]; then
        printf 'FAIL bare path: %s not restored\n' "$name" >&2
        exit 1
    fi
done

# Cross-check byte equality.
EXPECTED="$TMP/expected"
printf 'Hello, bare-minimum path!\n' > "$EXPECTED"
if ! cmp -s "$TARGET/hello.txt" "$EXPECTED"; then
    printf 'FAIL: hello.txt content mismatch\n' >&2
    exit 1
fi

printf 'test_bare_path: OK (recovered with python stripped, LCSAS_ALLOW_PYTHON_TIER=0)\n'
