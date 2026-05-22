#!/usr/bin/env python3
"""Scaling-benchmark driver for lcsas_blob_index_find (Phase 10).

For each N in a configurable sweep, generates a stress fixture with N
orphan blob index entries, invokes lcsas-restore with
LCSAS_STRESS_LOOKUPS=1000, and parses the bench line to extract
load_index_ms, rss_after_index_kib, and find_ns_mean.

Writes a markdown table to recovery/build/scaling.md and stdout.

Usage:
    python3 recovery/scripts/scaling_bench.py
    python3 recovery/scripts/scaling_bench.py --sizes 100,1000,10000

This script is opt-in (not invoked by `make gate`).  It needs:
  - the `lcsas-restore` binary built (any flavour)
  - the gen_fixture.py fixture generator
  - /scratch (or LCSAS_BENCH_TMP) writable for intermediate fixtures
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_SIZES = [100, 1_000, 10_000, 100_000, 1_000_000]
BENCH_LINE = re.compile(
    r"\[bench\] entries=(\d+) "
    r"load_index_ms=(\d+) "
    r"rss_after_index_kib=(\d+) "
    r"find_ns_mean=(\d+) "
    r"lookups=(\d+)"
)


def find_binary() -> Path:
    override = os.environ.get("LCSAS_RESTORE_BIN")
    if override:
        p = Path(override)
        if p.is_file() and os.access(p, os.X_OK):
            return p
    candidates = [
        REPO_ROOT / "recovery" / "build" / "lcsas-restore",
        REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    raise RuntimeError("no lcsas-restore binary found; run `make -C recovery`")


def bench_one(binary: Path, fixture: Path, pwfile: Path) -> dict:
    env = os.environ.copy()
    env["LCSAS_STRESS_LOOKUPS"] = "1000"
    res = subprocess.run(
        [str(binary),
         "--repo", str(fixture),
         "--password-file", str(pwfile),
         "--list-snapshots"],
        env=env, capture_output=True, text=True, timeout=300,
    )
    m = BENCH_LINE.search(res.stderr)
    if not m:
        raise RuntimeError(
            f"bench line not found in stderr:\n{res.stderr[:500]}"
        )
    return {
        "entries":           int(m.group(1)),
        "load_index_ms":     int(m.group(2)),
        "rss_after_kib":     int(m.group(3)),
        "find_ns_mean":      int(m.group(4)),
        "lookups":           int(m.group(5)),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES),
                    help="comma-separated N values to benchmark")
    p.add_argument("--tmpdir", default=os.environ.get("LCSAS_BENCH_TMP", "/scratch"),
                    help="parent directory for ephemeral fixtures")
    p.add_argument("--out", default=str(REPO_ROOT / "recovery" / "build" / "scaling.md"),
                    help="markdown output path")
    args = p.parse_args()

    sizes = sorted(int(x) for x in args.sizes.split(","))
    binary = find_binary()
    tmproot = Path(args.tmpdir)
    tmproot.mkdir(parents=True, exist_ok=True)

    pwfile = tmproot / "lcsas_bench_pw"
    pwfile.write_text("test")

    gen = REPO_ROOT / "recovery" / "tests" / "fixtures" / "gen_fixture.py"

    rows = []
    for n in sizes:
        fixture = tmproot / f"lcsas_bench_{n}"
        if fixture.exists():
            shutil.rmtree(fixture)
        print(f"[scaling_bench] N={n}: generating fixture...", flush=True)
        t0 = time.time()
        gen_res = subprocess.run(
            ["python3", str(gen), str(fixture),
             "--stress", str(n), "0", "1"],
            capture_output=True, text=True, timeout=600,
        )
        if gen_res.returncode != 0:
            print(f"  gen failed:\n{gen_res.stderr[:500]}", file=sys.stderr)
            return 1
        gen_secs = time.time() - t0
        print(f"  fixture ready in {gen_secs:.1f}s", flush=True)

        t0 = time.time()
        result = bench_one(binary, fixture, pwfile)
        bench_secs = time.time() - t0
        result["bench_wall_s"] = round(bench_secs, 2)
        result["gen_wall_s"] = round(gen_secs, 2)
        rows.append(result)
        print(f"  load={result['load_index_ms']}ms "
              f"rss={result['rss_after_kib']}KiB "
              f"find={result['find_ns_mean']}ns "
              f"(bench {bench_secs:.1f}s)", flush=True)
        shutil.rmtree(fixture)

    # Markdown report
    md = ["# lcsas_blob_index_find scaling benchmark", ""]
    md.append("Date: {}".format(time.strftime("%Y-%m-%d %H:%M:%S")))
    md.append("Host: {}".format(os.uname().nodename))
    md.append("Binary: {}".format(binary))
    md.append("")
    md.append("| N (entries) | load_index_ms | RSS (KiB) | find_ns_mean | gen_s | bench_s |")
    md.append("|------------:|--------------:|----------:|-------------:|------:|--------:|")
    for r in rows:
        md.append(
            "| {:>11,} | {:>13,} | {:>9,} | {:>12,} | {:>5.1f} | {:>7.1f} |".format(
                r["entries"], r["load_index_ms"], r["rss_after_kib"],
                r["find_ns_mean"], r["gen_wall_s"], r["bench_wall_s"]
            )
        )
    md.append("")
    md.append("**Interpretation**: `find_ns_mean` should scale roughly linearly with N "
              "because `lcsas_blob_index_find` is an O(n) linear scan. "
              "At true petabyte scale (~900M entries), per-lookup cost extrapolates "
              "to ~1 s — making restores impractical without an index-lookup fix. "
              "See repo.c:300-310.")
    out = "\n".join(md) + "\n"
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(out)
    print("\n" + out)
    print(f"\nWrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
