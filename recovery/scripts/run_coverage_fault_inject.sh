#!/bin/sh
# Coverage fault-injection sweep.
#
# For each test binary, runs it under malloc_inject.so + LCSAS_FAIL_AT=N
# for N = 1..max(total_allocs, MAX_N).  The fault-tolerant gcov shim
# (LCSAS_FAULT_INJECT_GCOV=1) flushes .gcda before _exit() even on
# signals, so coverage data accumulates.
#
# Usage: scripts/run_coverage_fault_inject.sh [MAX_N]
# Default MAX_N=200.
#
# Should run AFTER `make coverage-c` has built the instrumented test
# binaries.  Re-run gcovr after this to see the new coverage.
#
# PER_RUN_TIMEOUT (seconds, default 300): each fault-injected run is
# capped by `timeout` so a single non-terminating injection point cannot
# wedge the whole sweep.  One test_ecc allocation-failure injection loops
# forever locally (a suspected infinite retry-on-alloc-failure — see
# docs/adr/0001, issue #383); CI happens to grind through it, but locally
# the uncapped loop never returns.  300 s sits well above a full
# instrumented run (uninstrumented test_ecc ~37 s; instrument+gcov ~2-4x),
# so only a genuinely stuck run is killed — its coverage point is simply
# dropped locally and reconciled against CI.  The counting run (LCSAS_FAIL_AT
# unset) is deliberately NOT capped: it must run to completion to count
# allocations.

set -eu

MAX_N=${1:-200}
PER_RUN_TIMEOUT=${PER_RUN_TIMEOUT:-300}
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
LIB="$HERE/build/malloc_inject.so"

if [ ! -x "$LIB" ]; then
    echo "[cov-fi] need malloc_inject.so — re-run after coverage-c" >&2
    exit 1
fi

# `timeout` (coreutils) caps each injected run; degrade gracefully if it
# is missing (e.g. a minimal recovery host) rather than aborting the sweep.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT="timeout ${PER_RUN_TIMEOUT}"
else
    echo "[cov-fi] warning: 'timeout' not found; running without a per-run cap" >&2
    TIMEOUT=""
fi

export LD_PRELOAD="$LIB"
export LCSAS_FAULT_INJECT_GCOV=1
export LCSAS_FAIL_QUIET=1

for bin in "$HERE"/build/test_*; do
    [ -x "$bin" ] || continue
    case "$(basename "$bin")" in
        *.o|*.gcda|*.gcno) continue ;;
    esac
    # Count allocations once.  LCSAS_FAIL_AT unset → never fault.
    # Temporarily clear LCSAS_FAIL_QUIET so the count message lands on stderr.
    unset LCSAS_FAIL_AT
    total=$(env -u LCSAS_FAIL_QUIET "$bin" 2>&1 >/dev/null | grep -oP 'total allocations: \K\d+' || true)
    if [ "${total:-0}" = "0" ]; then
        echo "[cov-fi] $(basename "$bin"): no allocations counted, skipping"
        continue
    fi
    n_iter=$total
    if [ "$n_iter" -gt "$MAX_N" ]; then n_iter=$MAX_N; fi
    echo "[cov-fi] $(basename "$bin"): sweeping N=1..$n_iter (total=$total)"
    n=1
    while [ $n -le $n_iter ]; do
        LCSAS_FAIL_AT=$n $TIMEOUT "$bin" >/dev/null 2>&1 || true
        n=$((n + 1))
    done
done

echo "[cov-fi] sweep complete"
