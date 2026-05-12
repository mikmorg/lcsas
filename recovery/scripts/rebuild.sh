#!/bin/sh
# rebuild.sh -- bootstrap lcsas-restore from C sources alone.
#
# Use this when no prebuilt binary works on the host architecture.
# Locates any available C compiler and builds the recovery binary.
set -eu

ROOT="${1:-$(pwd)}"
if [ ! -f "$ROOT/Makefile" ] || [ ! -d "$ROOT/src" ]; then
    printf 'usage: %s RECOVERY_ROOT\n' "$0" >&2
    printf 'must contain Makefile and src/ subdirectory\n' >&2
    exit 2
fi

CC=""
for cand in cc gcc clang tcc pcc; do
    if command -v "$cand" >/dev/null 2>&1; then CC="$cand"; break; fi
done
if [ -z "$CC" ]; then
    printf 'no C compiler found on PATH; cannot rebuild\n' >&2
    exit 1
fi

printf 'rebuilding with %s in %s\n' "$CC" "$ROOT" >&2
cd "$ROOT"
make CC="$CC" -j2 build/lcsas-restore

OUT="$ROOT/build/lcsas-restore"
if [ -x "$OUT" ]; then
    printf 'built: %s\n' "$OUT"
    exit 0
fi
printf 'build did not produce %s\n' "$OUT" >&2
exit 1
