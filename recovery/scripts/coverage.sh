#!/bin/sh
# coverage.sh — convenience wrapper for the C coverage target.
#
# Usage:
#   sh recovery/scripts/coverage.sh
#
# Runs `make -C recovery coverage-c` and prints the LINE_COVERAGE
# line from recovery/build/coverage.txt.

set -e

SCRIPT_DIR=$(dirname "$0")
RECOVERY_DIR=$(cd "${SCRIPT_DIR}/.." && pwd)

make -C "${RECOVERY_DIR}" coverage-c

COV_TXT="${RECOVERY_DIR}/build/coverage.txt"
if [ -f "${COV_TXT}" ]; then
    grep '^LINE_COVERAGE=' "${COV_TXT}"
else
    echo "[coverage.sh] WARNING: coverage.txt not found at ${COV_TXT}" >&2
fi
