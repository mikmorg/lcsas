#!/usr/bin/env python3
"""Measure the cost of one password guess against a restic key file (#489).

FUP-03 Part 2 asked for brute-force numbers from data, not folklore.
This times scrypt at the parameters real writers record and prints a
markdown table of guesses per CPU-core-second and the core-years needed
to search half of a password space of a given entropy.

Usage:
    tools/kdf_bench.py                      # rustic + restic defaults
    tools/kdf_bench.py --params 131072,8,1  # one N,r,p triple
    tools/kdf_bench.py --keys-dir REPO/keys # the params a repo records

Single-threaded on purpose: an attacker's throughput is this rate times
the cores (or memory bandwidth) they can afford.  scrypt at these
settings needs 128*r*N bytes per guess (16 MiB at N=2^14, 128 MiB at
N=2^17), so it is memory-bound on any hardware.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import time
from pathlib import Path

# (label, N, r, p).  rustic 0.11.2 writes a fixed N=2^17; restic 0.19
# calibrates and was observed writing N=2^15 with p=1 or p=2.
DEFAULTS = [
    ("rustic 0.11 default", 1 << 17, 8, 1),
    ("restic 0.19 calibrated (p=1)", 1 << 15, 8, 1),
    ("restic 0.19 calibrated (p=2)", 1 << 15, 8, 2),
]
ENTROPY_BITS = (40, 50, 60, 70, 80, 90)
SECONDS_PER_YEAR = 365.25 * 24 * 3600


def seconds_per_guess(n: int, r: int, p: int, rounds: int) -> float:
    salt = os.urandom(64)
    maxmem = 128 * r * (n + p + 3) + (1 << 20)
    best = float("inf")
    for i in range(rounds):
        pw = f"guess-{i}".encode()
        t0 = time.perf_counter()
        hashlib.scrypt(pw, salt=salt, n=n, r=r, p=p, dklen=64, maxmem=maxmem)
        best = min(best, time.perf_counter() - t0)
    return best


def _cpu_model() -> str:
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return platform.processor() or platform.machine()


def params_from_keys(keys_dir: Path) -> list[tuple[str, int, int, int]]:
    out = []
    for kf in sorted(keys_dir.iterdir()):
        doc = json.loads(kf.read_text())
        out.append((kf.name[:12], int(doc["N"]), int(doc["r"]), int(doc["p"])))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--params", help="N,r,p (comma-separated)")
    ap.add_argument("--keys-dir", type=Path)
    ap.add_argument("--rounds", type=int, default=5,
                    help="timed guesses per setting; the fastest is kept")
    args = ap.parse_args()

    if args.params:
        n, r, p = (int(x) for x in args.params.split(","))
        settings = [(args.params, n, r, p)]
    elif args.keys_dir:
        settings = params_from_keys(args.keys_dir)
    else:
        settings = DEFAULTS

    print(f"Host: {_cpu_model()}, "
          f"{os.cpu_count()} cores, Python {platform.python_version()}, "
          f"OpenSSL scrypt (hashlib); single thread, best of {args.rounds}.")
    print()
    print("| setting | N | r | p | MiB/guess | s/guess | guesses/core-s |")
    print("|---|---|---|---|---|---|---|")
    rates = {}
    for label, n, r, p in settings:
        s = seconds_per_guess(n, r, p, args.rounds)
        rates[label] = 1.0 / s
        print(f"| {label} | {n} | {r} | {p} | {128 * r * n / 2**20:.0f} | "
              f"{s:.3f} | {1.0 / s:.1f} |")
    print()
    head = " | ".join(rates)
    print(f"| entropy | half the space (guesses) | core-years: {head} |")
    print("|---|---|" + "---|" * len(rates))
    for bits in ENTROPY_BITS:
        guesses = 2.0 ** (bits - 1)
        years = " | ".join(f"{guesses / rate / SECONDS_PER_YEAR:.2g}"
                           for rate in rates.values())
        print(f"| {bits} bits | {guesses:.2g} | {years} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
