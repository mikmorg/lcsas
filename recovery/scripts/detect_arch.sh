#!/bin/sh
# detect_arch.sh -- normalize uname -m output.
#
# POSIX-sh.  Prints one of: x86_64 aarch64 riscv64
# Exits 1 if architecture is not supported.
set -eu

m="$(uname -m)"
case "$m" in
    x86_64|amd64)  printf 'x86_64\n' ;;
    aarch64|arm64) printf 'aarch64\n' ;;
    riscv64)       printf 'riscv64\n' ;;
    *)             printf 'unsupported: %s\n' "$m" >&2; exit 1 ;;
esac
