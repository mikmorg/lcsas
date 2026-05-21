#!/usr/bin/env python3
"""Check per-file line coverage against a minimum threshold.

Reads the coverage.json produced by `make coverage-c` and exits non-zero
if any .c file under recovery/src/lcsas-restore/ falls below the threshold.

Usage:
    python3 recovery/scripts/coverage_check.py [--threshold N]
    python3 recovery/scripts/coverage_check.py --threshold 95
"""
import argparse
import json
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--threshold",
        type=float,
        default=95.0,
        help="minimum per-file line coverage %% (default: 95)",
    )
    parser.add_argument(
        "--json",
        dest="json_path",
        default=None,
        help="path to coverage.json (default: recovery/build/coverage.json)",
    )
    parser.add_argument(
        "--exclude",
        default="",
        help="comma-separated basenames to skip for threshold check (still reported)",
    )
    args = parser.parse_args()
    excluded = set(args.exclude.split(",")) if args.exclude else set()

    repo_root = Path(__file__).resolve().parents[2]
    json_path = Path(args.json_path) if args.json_path else repo_root / "recovery" / "build" / "coverage.json"

    if not json_path.exists():
        print(f"[coverage_check] ERROR: {json_path} not found — run `make -C recovery coverage-c` first", file=sys.stderr)
        return 1

    with open(json_path) as f:
        data = json.load(f)

    # gcovr JSON summary format: top-level line_percent + per-file list in "files"
    files = data.get("files", [])
    if not files:
        print("[coverage_check] ERROR: no file entries in coverage.json", file=sys.stderr)
        return 1

    failures = []
    rows = []
    for entry in files:
        filename = entry.get("filename", "")
        # filenames may be relative (e.g. "src/lcsas-restore/aes.c") or absolute
        if "src/lcsas-restore/" not in filename:
            continue
        # Normalise to a short relative display name
        try:
            short = str(Path(filename).relative_to(repo_root))
        except ValueError:
            short = filename
        basename = Path(filename).name
        line_pct = entry.get("line_percent", 0.0)
        excluded_flag = basename in excluded
        rows.append((short, line_pct, excluded_flag))
        if line_pct < args.threshold and not excluded_flag:
            failures.append((short, line_pct))

    if not rows:
        print("[coverage_check] WARNING: no src/lcsas-restore/*.c files found in report", file=sys.stderr)
        return 0

    max_name = max(len(r[0]) for r in rows)
    print(f"\n{'File':<{max_name}}  {'Line%':>7}  {'Status':>12}")
    print("-" * (max_name + 24))
    for name, pct, excl in sorted(rows):
        if excl:
            status = "EXCLUDED"
        elif pct >= args.threshold:
            status = "OK"
        else:
            status = f"FAIL (<{args.threshold:.0f}%)"
        print(f"{name:<{max_name}}  {pct:>6.1f}%  {status:>12}")

    print()
    if failures:
        print(f"[coverage_check] FAIL: {len(failures)} file(s) below {args.threshold:.0f}% threshold:", file=sys.stderr)
        for name, pct in failures:
            print(f"  {name}: {pct:.1f}%", file=sys.stderr)
        return 1

    overall = data.get("line_percent", 0.0)
    print(f"[coverage_check] PASS: all {len(rows)} files >= {args.threshold:.0f}%  (overall: {overall:.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
