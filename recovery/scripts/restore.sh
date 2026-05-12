#!/bin/sh
# restore.sh -- POSIX-sh driver for the LCSAS recovery cascade.
#
# Replaces the Python-generated RESTORE_SCRIPT in src/lcsas/meta/builder.py.
# Cascading recovery strategy:
#
#   1.  bin/<arch>/lcsas-restore        (primary, prebuilt static C89)
#   2.  bin/rustic-static               (vendored, if shipped)
#   3.  Rebuild lcsas-restore from src/ (any cc)
#   4.  python3 standalone_restorer.py  (last resort)
#
# Usage from a mounted recovery medium:
#   sh /mnt/recovery/scripts/restore.sh /mnt/recovery TARGET_DIR
#
# Where /mnt/recovery is the root of the LCSAS recovery tree and
# TARGET_DIR is where to write restored files.  Reads password from
# stdin (or LCSAS_PASSWORD env var if set non-empty).

set -eu

if [ $# -lt 2 ]; then
    cat >&2 <<EOF
usage: $0 RECOVERY_ROOT TARGET_DIR [SNAPSHOT_ID|latest]

RECOVERY_ROOT must contain:
  bin/<arch>/lcsas-restore   prebuilt binary
  src/                       C source for rebuild fallback
  Makefile                   build recipe

Password is read from stdin or \$LCSAS_PASSWORD.
EOF
    exit 2
fi

RECOVERY="$1"
TARGET="$2"
SNAP="${3:-latest}"

# Resolve arch.
ARCH="$(sh "$RECOVERY/scripts/detect_arch.sh" 2>/dev/null || uname -m)"
case "$ARCH" in
    x86_64|amd64)        ARCH=x86_64 ;;
    aarch64|arm64)       ARCH=aarch64 ;;
    riscv64)             ARCH=riscv64 ;;
    *)
        printf 'unsupported arch: %s\n' "$ARCH" >&2
        exit 1 ;;
esac

# Locate or write a temporary password file.
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
        # POSIX read; no echo disable.  Recovery context: terminal is local.
        IFS= read -r pw
        printf '%s\n' "$pw" > "$PWFILE"
    fi
fi

cleanup() {
    [ -n "$PWFILE_TMP" ] && [ -f "$PWFILE_TMP" ] && rm -f "$PWFILE_TMP"
}
trap cleanup EXIT INT TERM

REPO="$RECOVERY/repo"
if [ ! -d "$REPO" ]; then
    # Some layouts put the restic tree directly at the recovery root.
    if [ -d "$RECOVERY/keys" ] && [ -d "$RECOVERY/index" ]; then
        REPO="$RECOVERY"
    else
        printf 'no restic repo found under %s\n' "$RECOVERY" >&2
        exit 1
    fi
fi

# Tier 1: prebuilt lcsas-restore.
RESTORE_BIN="$RECOVERY/bin/$ARCH/lcsas-restore"
if [ -x "$RESTORE_BIN" ]; then
    printf 'using prebuilt lcsas-restore (%s)\n' "$ARCH" >&2
    exec "$RESTORE_BIN" --repo "$REPO" --password-file "$PWFILE" \
                       --target "$TARGET" --snapshot "$SNAP"
fi

# Tier 2: vendored rustic-static.
RUSTIC_BIN="$RECOVERY/bin/$ARCH/rustic-static"
if [ -x "$RUSTIC_BIN" ]; then
    printf 'using vendored rustic-static (%s)\n' "$ARCH" >&2
    exec "$RUSTIC_BIN" --repository "$REPO" --password-file "$PWFILE" \
                     restore "$SNAP" "$TARGET"
fi

# Tier 3: rebuild from source.
if [ -d "$RECOVERY/src" ] && [ -f "$RECOVERY/Makefile" ]; then
    if command -v cc >/dev/null 2>&1 \
            || command -v gcc >/dev/null 2>&1 \
            || command -v clang >/dev/null 2>&1; then
        printf 'no prebuilt binary; rebuilding lcsas-restore from source\n' >&2
        ( cd "$RECOVERY" && make -j2 build/lcsas-restore )
        BUILT="$RECOVERY/build/lcsas-restore"
        if [ -x "$BUILT" ]; then
            exec "$BUILT" --repo "$REPO" --password-file "$PWFILE" \
                          --target "$TARGET" --snapshot "$SNAP"
        fi
    fi
fi

# Tier 4: Python fallback.
PYBIN=""
for p in python3 python; do
    if command -v "$p" >/dev/null 2>&1; then PYBIN="$p"; break; fi
done
PYREST="$RECOVERY/standalone_restorer.py"
if [ -n "$PYBIN" ] && [ -f "$PYREST" ]; then
    printf 'falling back to standalone_restorer.py\n' >&2
    exec "$PYBIN" "$PYREST" "$REPO" "$TARGET" --password-file "$PWFILE"
fi

cat >&2 <<EOF
ERROR: no recovery method available.

Tried:
  $RESTORE_BIN
  $RUSTIC_BIN
  rebuild from $RECOVERY/src
  $PYREST

Install any one of:
  - a C compiler (cc/gcc/clang) to rebuild from source
  - python3 to run the pure-Python fallback
EOF
exit 1
