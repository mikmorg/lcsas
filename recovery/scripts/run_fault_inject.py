#!/usr/bin/env python3
"""Sweep N=1..total across malloc_inject.so to catch fault-handling crashes.

For each test binary under recovery/build/test_*:
  1. Run with LD_PRELOAD=malloc_inject.so to count total allocations.
  2. For N in 1..total, run with LCSAS_FAIL_AT=N and check for crash signals.
     Any exit code in {139, -11, 134, -6} (SEGV/ABORT) = FAIL.
     Hang past TIMEOUT seconds = FAIL.
  3. Report binaries with crashes.

When `--coverage` is passed (or the test binaries are coverage-instrumented),
each run accumulates .gcda data, so the final `gcovr` report covers every
malloc-failure branch swept.

Usage:
    python3 recovery/scripts/run_fault_inject.py
    python3 recovery/scripts/run_fault_inject.py --binaries test_repo,test_catalog
    python3 recovery/scripts/run_fault_inject.py --max-sweep 200  (smoke run)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

CRASH_RCS = {-11, 139, -6, 134, -7, -8, -9}  # SEGV / ABORT / KILL / BUS / FPE
TIMEOUT_SEC = 30


def find_repo_root() -> Path:
    here = Path(__file__).resolve()
    for parent in here.parents:
        if (parent / "recovery" / "Makefile").exists():
            return parent
    raise RuntimeError("could not locate repo root")


def count_allocations(lib: Path, binary: Path) -> int:
    env = os.environ.copy()
    env["LD_PRELOAD"] = str(lib)
    res = subprocess.run([str(binary)], env=env, capture_output=True,
                          text=True, timeout=TIMEOUT_SEC)
    m = re.search(r"total allocations: (\d+)", res.stderr)
    return int(m.group(1)) if m else 0


def sweep(lib: Path, binary: Path, total: int, max_n: int) -> list[tuple[int, int, str]]:
    """Return list of (N, rc, stderr_excerpt) for any crash."""
    crashes = []
    env = os.environ.copy()
    env["LD_PRELOAD"] = str(lib)
    env["LCSAS_FAIL_QUIET"] = "1"
    n_iter = min(total, max_n)
    for n in range(1, n_iter + 1):
        env["LCSAS_FAIL_AT"] = str(n)
        try:
            res = subprocess.run([str(binary)], env=env,
                                  capture_output=True, text=True,
                                  timeout=TIMEOUT_SEC)
            if res.returncode in CRASH_RCS:
                crashes.append((n, res.returncode, res.stderr[:400]))
        except subprocess.TimeoutExpired:
            crashes.append((n, -1, "TIMEOUT"))
    return crashes


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--binaries", default="",
                    help="comma-separated test binary basenames (default: all test_*)")
    p.add_argument("--max-sweep", type=int, default=10000,
                    help="maximum N to sweep per binary (default: 10000)")
    args = p.parse_args()

    root = find_repo_root()
    build = root / "recovery" / "build"
    lib = build / "malloc_inject.so"
    if not lib.exists():
        print(f"[fault-inject] ERROR: {lib} not built — run:", file=sys.stderr)
        print("  cc -shared -fPIC -O0 -g -D_GNU_SOURCE \\", file=sys.stderr)
        print("     recovery/scripts/malloc_inject.c \\", file=sys.stderr)
        print(f"     -o {lib.relative_to(root)} -ldl", file=sys.stderr)
        return 1

    if args.binaries:
        bins = [build / b for b in args.binaries.split(",")]
    else:
        bins = sorted(build.glob("test_*"))
        # exclude .o, .gcda, etc.
        bins = [b for b in bins if b.is_file() and os.access(b, os.X_OK)
                and "." not in b.name]

    total_crashes = 0
    print(f"[fault-inject] sweeping {len(bins)} test binaries (max N={args.max_sweep})")
    for binary in bins:
        t0 = time.time()
        total = count_allocations(lib, binary)
        if total == 0:
            print(f"  {binary.name:40s}  no allocations counted — skipped")
            continue
        n_iter = min(total, args.max_sweep)
        crashes = sweep(lib, binary, total, args.max_sweep)
        elapsed = time.time() - t0
        status = "OK" if not crashes else f"FAIL ({len(crashes)} crashes)"
        print(f"  {binary.name:40s}  total={total:5d}  swept={n_iter:5d}  "
              f"{elapsed:5.1f}s  {status}")
        for n, rc, err in crashes[:5]:
            print(f"    alloc #{n}  rc={rc}  stderr: {err[:120]!r}")
        if crashes:
            total_crashes += len(crashes)

    print()
    if total_crashes:
        print(f"[fault-inject] FAIL: {total_crashes} crash(es) across all binaries", file=sys.stderr)
        return 1
    print(f"[fault-inject] PASS: 0 crashes across all binaries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
