#!/bin/sh
# detect_arch.sh -- normalize uname -m output.
#
# POSIX-sh.  Prints x86_64, aarch64 or riscv64; exits 1 for any other
# machine, and restore.sh then falls back to `uname -m`.  restore.sh's
# own target table decides what is supported: armv7 is (it reaches the
# table through that fallback); riscv64 is NOT one of the six approved
# targets, so restore.sh stops with "unsupported Linux machine".
set -eu

m="$(uname -m)"
case "$m" in
    x86_64|amd64)  printf 'x86_64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    riscv64)       printf 'riscv64\n' ;;
    *)             printf 'unsupported: %s\n' "$m" >&2; exit 1 ;;
esac
