#!/bin/sh
# restore.sh -- POSIX-sh driver for the LCSAS recovery cascade.
#
# Bare-minimum recovery is C89 + POSIX sh ONLY.  Python is NOT on the
# bare path; it lives on a separate tier (5) that is only reached when
# every C-based option has failed.
#
# Cascade (bare-minimum path = tiers 1-4):
#
#   Tier 1.  bin/<arch>/lcsas-restore        prebuilt static C89 binary
#   Tier 2.  bin/<arch>/rustic-static        vendored Rust binary (cross-check)
#   Tier 3.  Rebuild lcsas-restore from src/ (any C compiler; POSIX make)
#   Tier 4.  rustic-built-from-vendored-rust (requires cargo, deferred)
#   ----- Bare minimum stops here.  None of the above need Python. -----
#   Tier 5.  python3 standalone_restorer.py  (only if all above failed)
#
# The script can be invoked two ways:
#
#   1. Auto-locate from script path:
#        sh /path/to/restore.sh [TARGET_DIR] [SNAPSHOT_ID|latest]
#      Recovery root is auto-detected from $0's location.
#
#   2. Explicit:
#        sh restore.sh RECOVERY_ROOT TARGET_DIR [SNAPSHOT_ID|latest]
#
# Inputs:
#   $LCSAS_PASSWORD  -- if set, used as password (otherwise prompts on stdin)
#   $LCSAS_PWFILE    -- if set, path to a password file (overrides above)

set -eu

# ── Argument handling and auto-discovery ──────────────────────────

SCRIPT="$(
    # POSIX-portable realpath approximation.
    cd "$(dirname "$0")" 2>/dev/null && pwd -P
)/$(basename "$0")"
SCRIPT_DIR="$(dirname "$SCRIPT")"

# When invoked as $RECOVERY/scripts/restore.sh, RECOVERY is parent dir.
# When invoked as $META/restore.sh (top-level), RECOVERY is $META/recovery.
AUTO_RECOVERY=""
if [ -d "$SCRIPT_DIR/../scripts" ] && [ "$SCRIPT_DIR" != "$SCRIPT_DIR/../scripts" ]; then
    AUTO_RECOVERY="$(cd "$SCRIPT_DIR/.." && pwd -P)"
elif [ -d "$SCRIPT_DIR/recovery/scripts" ]; then
    AUTO_RECOVERY="$SCRIPT_DIR/recovery"
fi

RECOVERY=""
TARGET=""
SNAP="latest"

# Pattern 1: first arg looks like a recovery root (has bin/ or src/).
if [ $# -ge 2 ] && [ -d "$1/bin" -o -d "$1/src" ] 2>/dev/null; then
    RECOVERY="$1"
    TARGET="$2"
    SNAP="${3:-latest}"
elif [ $# -ge 1 ] && [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="$1"
    SNAP="${2:-latest}"
elif [ -n "$AUTO_RECOVERY" ]; then
    RECOVERY="$AUTO_RECOVERY"
    TARGET="${TARGET:-/tmp/restored}"
else
    cat >&2 <<EOF
usage: $0 [RECOVERY_ROOT] TARGET_DIR [SNAPSHOT_ID|latest]

  RECOVERY_ROOT (auto-detected when restore.sh is run from inside the
  recovery tree) must contain bin/<arch>/lcsas-restore and/or src/.
  TARGET_DIR is where to write restored files (default: /tmp/restored).

  Password is read from stdin, \$LCSAS_PASSWORD env, or \$LCSAS_PWFILE.
EOF
    exit 2
fi

# ── Architecture detection ────────────────────────────────────────

if [ -x "$RECOVERY/scripts/detect_arch.sh" ]; then
    ARCH="$(sh "$RECOVERY/scripts/detect_arch.sh" 2>/dev/null || uname -m)"
else
    ARCH="$(uname -m)"
fi
case "$ARCH" in
    x86_64|amd64)        ARCH=x86_64 ;;
    aarch64|arm64)       ARCH=aarch64 ;;
    riscv64)             ARCH=riscv64 ;;
    *)
        printf 'unsupported arch: %s\n' "$ARCH" >&2
        exit 1 ;;
esac

# ── Password file ─────────────────────────────────────────────────

PWFILE="${LCSAS_PWFILE:-}"
PWFILE_TMP=""
if [ -z "$PWFILE" ]; then
    PWFILE_TMP="$(mktemp /tmp/lcsas-pw.XXXXXX)"
    chmod 600 "$PWFILE_TMP"
    PWFILE="$PWFILE_TMP"
    if [ -n "${LCSAS_PASSWORD:-}" ]; then
        printf '%s\n' "$LCSAS_PASSWORD" > "$PWFILE"
    else
        printf 'Password: ' >&2
        IFS= read -r pw
        printf '%s\n' "$pw" > "$PWFILE"
    fi
fi
cleanup() {
    [ -n "$PWFILE_TMP" ] && [ -f "$PWFILE_TMP" ] && rm -f "$PWFILE_TMP"
}
trap cleanup EXIT INT TERM

# ── Repo discovery ────────────────────────────────────────────────

REPO=""
for candidate in "$RECOVERY/repo" "$RECOVERY"; do
    if [ -d "$candidate/keys" ] && [ -d "$candidate/index" ]; then
        REPO="$candidate"
        break
    fi
done
if [ -z "$REPO" ]; then
    printf 'no restic repo (with keys/ and index/) found under %s\n' \
           "$RECOVERY" >&2
    exit 1
fi

mkdir -p "$TARGET"

# ── Tier 1: prebuilt lcsas-restore (C89, static, no Python) ───────

RESTORE_BIN="$RECOVERY/bin/$ARCH/lcsas-restore"
if [ -x "$RESTORE_BIN" ]; then
    printf '[tier 1] using prebuilt lcsas-restore (%s)\n' "$ARCH" >&2
    exec "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET" --snapshot "$SNAP"
fi

# ── Tier 2: vendored rustic-static (no Python) ────────────────────

RUSTIC_BIN="$RECOVERY/bin/$ARCH/rustic-static"
if [ -x "$RUSTIC_BIN" ]; then
    printf '[tier 2] using vendored rustic-static (%s)\n' "$ARCH" >&2
    exec "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET"
fi

# ── Tier 3: rebuild lcsas-restore from C source (no Python) ───────

if [ -d "$RECOVERY/src" ] && [ -f "$RECOVERY/Makefile" ]; then
    CC=""
    for cand in cc gcc clang tcc pcc; do
        if command -v "$cand" >/dev/null 2>&1; then CC="$cand"; break; fi
    done
    if [ -n "$CC" ]; then
        printf '[tier 3] rebuilding lcsas-restore from source with %s\n' \
               "$CC" >&2
        ( cd "$RECOVERY" && make CC="$CC" -j2 build/lcsas-restore )
        BUILT="$RECOVERY/build/lcsas-restore"
        if [ -x "$BUILT" ]; then
            exec "$BUILT" --repo "$REPO" --password-file "$PWFILE" \
                          --target "$TARGET" --snapshot "$SNAP"
        fi
    fi
fi

# ── Tier 4: rebuild rustic from vendored Rust source ──────────────
# Only reached if no C compiler is available.  Rust toolchain has heavy
# requirements; we attempt it as a last C-free option.

if [ -d "$RECOVERY/../vendored/rustic" ] && command -v cargo >/dev/null 2>&1; then
    printf '[tier 4] building rustic from vendored source (cargo)\n' >&2
    ( cd "$RECOVERY/../vendored/rustic" && cargo build --release --offline )
    BUILT="$RECOVERY/../vendored/rustic/target/release/rustic"
    if [ -x "$BUILT" ]; then
        exec "$BUILT" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET"
    fi
fi

# ─────────────────────────────────────────────────────────────────
# BARE-MINIMUM PATH ENDS HERE.  Everything above is C/Rust + POSIX sh,
# with NO Python dependency at any step.
# ─────────────────────────────────────────────────────────────────

# ── Tier 5: Python fallback (LAST RESORT, off the bare path) ─────

if [ "${LCSAS_ALLOW_PYTHON_TIER:-1}" = "1" ]; then
    PYBIN=""
    for p in python3 python; do
        if command -v "$p" >/dev/null 2>&1; then PYBIN="$p"; break; fi
    done
    PYREST=""
    for cand in "$RECOVERY/../standalone_restorer.py" \
                "$RECOVERY/standalone_restorer.py" \
                "$SCRIPT_DIR/../standalone_restorer.py"; do
        if [ -f "$cand" ]; then PYREST="$cand"; break; fi
    done
    if [ -n "$PYBIN" ] && [ -n "$PYREST" ]; then
        printf '[tier 5] falling back to Python (%s + %s)\n' \
               "$PYBIN" "$PYREST" >&2
        exec "$PYBIN" "$PYREST" "$REPO" "$TARGET" --password-file "$PWFILE"
    fi
fi

cat >&2 <<EOF
ERROR: no recovery method available.

The bare-minimum recovery path (tiers 1-4) needs ONE of:
  * a prebuilt $RESTORE_BIN
  * a prebuilt $RUSTIC_BIN
  * a C compiler (cc, gcc, clang, tcc, or pcc) to rebuild from source
  * a cargo toolchain to rebuild rustic from vendored source

The optional Python tier (tier 5) needs python3 and standalone_restorer.py.

See $RECOVERY/docs/RECOVER.txt for manual recovery instructions.
EOF
exit 1
