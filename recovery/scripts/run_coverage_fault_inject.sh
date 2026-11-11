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
#
# Cleanup: a faulted run leaves through the gcov shim's _exit() and a
# stuck one is killed by `timeout`, so a test's own cleanup at the end
# of main() never runs.  Each run therefore gets its own TMPDIR (made
# with mktemp under the caller's $TMPDIR, default /tmp), removed after
# the run.  test_disc_locator puts its drain caches in /dev/shm when
# TMPDIR has no room, so after each run any of our own
# /dev/shm/lcsas_dl_*_<pid> folders whose process is gone are removed
# too.  Before this, every sweep left hundreds of fixture folders in
# /tmp and /dev/shm (RAM), and a 24 MB drain case filled swap.
#
# COV_FI_TESTS (default: every build/test_*): space-separated test
# names to sweep, e.g. "test_disc_locator".  COV_FI_LIB (default
# build/malloc_inject.so): the LD_PRELOAD shim.  Both exist for the
# cleanup gate test (tests/recovery_hardening/
# test_coverage_fault_inject_cleanup.py).

set -eu

MAX_N=${1:-200}
PER_RUN_TIMEOUT=${PER_RUN_TIMEOUT:-300}
HERE="$(cd "$(dirname "$0")"/.. && pwd)"
LIB="${COV_FI_LIB:-$HERE/build/malloc_inject.so}"
SWEEP_TMP="${TMPDIR:-/tmp}"

if [ ! -x "$LIB" ]; then
    echo "[cov-fi] need malloc_inject.so — re-run after coverage-c" >&2
    exit 1
fi

# Remove our own /dev/shm/lcsas_dl_*_<pid> folders whose process has
# exited.  A live pid (a test still running, here or in another job)
# keeps its folder.
reap_shm() {
    for d in /dev/shm/lcsas_dl_*_*; do
        [ -d "$d" ] || continue
        [ -O "$d" ] || continue
        pid=${d##*_}
        case "$pid" in
            ''|*[!0-9]*) continue ;;
        esac
        if kill -0 "$pid" 2>/dev/null; then continue; fi
        rm -rf "$d"
    done
}

# A sweep stopped from outside (Ctrl-C, or an outer `timeout`/make
# killing it) used to skip the per-run cleanup below: 1,162 drain-cache
# folders were left in /dev/shm (RAM) after one interrupted sweep
# (2026-11-11).  Clean the current run's TMPDIR and reap /dev/shm on
# every way out.
run_tmp=""
cleanup() {
    if [ -n "$run_tmp" ]; then rm -rf "$run_tmp"; fi
    reap_shm
}
trap cleanup EXIT
trap 'cleanup; trap - EXIT; exit 130' INT
trap 'cleanup; trap - EXIT; exit 143' TERM

# `timeout` (coreutils) caps each injected run; degrade gracefully if it
# is missing (e.g. a minimal restore host) rather than aborting the sweep.
if command -v timeout >/dev/null 2>&1; then
    TIMEOUT="timeout ${PER_RUN_TIMEOUT}"
else
    echo "[cov-fi] warning: 'timeout' not found; running without a per-run cap" >&2
    TIMEOUT=""
fi

export LD_PRELOAD="$LIB"
export LCSAS_FAULT_INJECT_GCOV=1
export LCSAS_FAIL_QUIET=1

# The binaries to sweep, as the positional parameters.
if [ -n "${COV_FI_TESTS:-}" ]; then
    set --
    for t in $COV_FI_TESTS; do set -- "$@" "$HERE/build/$t"; done
else
    set -- "$HERE"/build/test_*
fi

for bin in "$@"; do
    [ -x "$bin" ] || continue
    case "$(basename "$bin")" in
        *.o|*.gcda|*.gcno) continue ;;
    esac
    # Count allocations once.  LCSAS_FAIL_AT unset → never fault.
    # Temporarily clear LCSAS_FAIL_QUIET so the count message lands on stderr.
    unset LCSAS_FAIL_AT
    run_tmp=$(mktemp -d "$SWEEP_TMP/lcsas-cov-fi.XXXXXX")
    total=$(TMPDIR="$run_tmp" env -u LCSAS_FAIL_QUIET "$bin" 2>&1 >/dev/null | grep -oP 'total allocations: \K\d+' || true)
    rm -rf "$run_tmp"
    reap_shm
    if [ "${total:-0}" = "0" ]; then
        echo "[cov-fi] $(basename "$bin"): no allocations counted, skipping"
        continue
    fi
    n_iter=$total
    if [ "$n_iter" -gt "$MAX_N" ]; then n_iter=$MAX_N; fi
    echo "[cov-fi] $(basename "$bin"): sweeping N=1..$n_iter (total=$total)"
    n=1
    while [ $n -le $n_iter ]; do
        run_tmp=$(mktemp -d "$SWEEP_TMP/lcsas-cov-fi.XXXXXX")
        rc=0
        LCSAS_FAIL_AT=$n TMPDIR="$run_tmp" $TIMEOUT "$bin" >/dev/null 2>&1 || rc=$?
        if [ "$rc" = 124 ] && [ -n "$TIMEOUT" ]; then
            echo "[cov-fi] $(basename "$bin") N=$n: stopped after ${PER_RUN_TIMEOUT} s"
        fi
        rm -rf "$run_tmp"
        reap_shm
        n=$((n + 1))
    done
done

echo "[cov-fi] sweep complete"
