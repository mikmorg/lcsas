#!/usr/bin/env python3
"""shellcheck ratchet for the POSIX recovery scripts (#484).

``recovery/scripts/restore.sh`` is the first thing an heir runs, and no
gate linted it.  Its existing findings are recorded in
``scripts/shellcheck_baseline.txt`` as ``<file> <code> <count>``: no line
numbers, so ordinary edits never invalidate the baseline.  The gate fails
when a (file, code) pair has MORE findings than the baseline (something
new) and when it has FEWER (the baseline is stale: lower it in the same
change, so a fixed warning can't silently come back).

Usage: shellcheck_gate.py FILE...           (CI / ``make shellcheck``)
       shellcheck_gate.py --print FILE...   (print a fresh baseline)
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

BASELINE = Path(__file__).with_name("shellcheck_baseline.txt")

Counts = Counter[tuple[str, str]]


def run_shellcheck(files: list[str]) -> Counts:
    """Count shellcheck findings per (file, SCnnnn) for POSIX sh."""
    proc = subprocess.run(
        ["shellcheck", "-s", "sh", "-f", "json1", *files],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode not in (0, 1):  # 1 = findings; anything else = error
        sys.exit(f"shellcheck failed (rc={proc.returncode}): {proc.stderr}")
    comments = json.loads(proc.stdout or '{"comments": []}')["comments"]
    return Counter((c["file"], f"SC{c['code']}") for c in comments)


def load_baseline(path: Path) -> Counts:
    counts: Counts = Counter()
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            file, code, n = line.split()
            counts[(file, code)] = int(n)
    return counts


def compare(found: Counts, baseline: Counts) -> list[str]:
    """Return one message per (file, code) that differs from the baseline."""
    problems = []
    for key in sorted(set(found) | set(baseline)):
        have, allowed = found[key], baseline[key]
        if have > allowed:
            problems.append(
                f"NEW: {key[0]} {key[1]}: {have} finding(s), baseline {allowed}"
            )
        elif have < allowed:
            problems.append(
                f"STALE BASELINE: {key[0]} {key[1]}: {have} finding(s), "
                f"baseline {allowed} -- lower it in {BASELINE.name}"
            )
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+")
    parser.add_argument("--print", action="store_true",
                        help="print a baseline for the current findings")
    args = parser.parse_args(argv)
    if shutil.which("shellcheck") is None:
        print("shellcheck is not installed", file=sys.stderr)
        return 2
    found = run_shellcheck(args.files)
    if args.print:
        for (file, code), n in sorted(found.items()):
            print(f"{file} {code} {n}")
        return 0
    problems = compare(found, load_baseline(BASELINE))
    for p in problems:
        print(p, file=sys.stderr)
    if problems:
        print("Run shellcheck -s sh on the file above for details.",
              file=sys.stderr)
        return 1
    print(f"shellcheck ratchet: {sum(found.values())} finding(s), "
          "all in the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
