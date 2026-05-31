#!/usr/bin/env python3
"""Enforce recovery/docs/EXEMPTIONS.md as a coverage gate.

Reads:
  - recovery/build/coverage.json (gcovr output from `make coverage-c`)
  - recovery/docs/EXEMPTIONS.md  (the parseable EXEMPTIONS-FENCE block)

Fails (non-zero exit) when either invariant is violated:
  1. An uncovered line is NOT listed in EXEMPTIONS  (someone added
     uncovered code without documenting it).
  2. An EXEMPTIONS entry refers to a line that IS now covered  (someone
     closed a gap and forgot to update the doc).

Wire into `make coverage-c` so the doc cannot silently drift.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
COVERAGE_JSON_DEFAULT = REPO_ROOT / "recovery" / "build" / "coverage.json"
EXEMPTIONS_MD_DEFAULT = REPO_ROOT / "recovery" / "docs" / "EXEMPTIONS.md"

FENCE_BEGIN = "<!-- EXEMPTIONS-FENCE-BEGIN -->"
FENCE_END = "<!-- EXEMPTIONS-FENCE-END -->"

# A row in the fenced block looks like:
#   file.c:NNN   CATEGORY   rationale...
ENTRY_RE = re.compile(
    r"^([A-Za-z0-9_]+\.c):(\d+)\s+(INTRACTABLE|DEFENSIVE|DEFERRED)\b"
)


def parse_exemptions(md_path: Path) -> set[tuple[str, int]]:
    """Return {(filename, line_no), ...} from the fenced block."""
    text = md_path.read_text(encoding="utf-8")
    try:
        block = text.split(FENCE_BEGIN, 1)[1].split(FENCE_END, 1)[0]
    except IndexError:
        raise SystemExit(
            f"[exemptions_check] {md_path} missing FENCE markers"
        )
    out = set()
    for raw in block.splitlines():
        line = raw.strip()
        # Skip comments, blanks, code fences, and continuation rows
        # (rows whose first token is just a closing-quote/repeat marker
        # like the rationale placeholders in the doc).
        if not line or line.startswith("#") or line.startswith("```"):
            continue
        m = ENTRY_RE.match(line)
        if not m:
            continue
        out.add((m.group(1), int(m.group(2))))
    return out


def parse_uncov(cov_json: Path) -> set[tuple[str, int]]:
    """Return {(basename, line_no), ...} for every count==0 source line
    in recovery/src/lcsas-restore/*.c."""
    data = json.loads(cov_json.read_text(encoding="utf-8"))
    out = set()
    for entry in data.get("files", []):
        path = entry.get("filename") or entry.get("file") or ""
        if "src/lcsas-restore/" not in path:
            continue
        base = path.rsplit("/", 1)[-1]
        # Both summary and detail JSON formats stash per-line counts
        # under "lines". Tolerate either schema.
        for ln in entry.get("lines", []):
            if ln.get("count", 0) == 0:
                out.add((base, ln["line_number"]))
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--coverage-json",
        type=Path,
        default=COVERAGE_JSON_DEFAULT,
    )
    p.add_argument(
        "--exemptions-md",
        type=Path,
        default=EXEMPTIONS_MD_DEFAULT,
    )
    args = p.parse_args()

    if not args.coverage_json.exists():
        # coverage.json missing means the summary JSON wasn't produced;
        # gcovr was not invoked (likely not installed).  Skip rather
        # than block the build.
        print(
            f"[exemptions_check] {args.coverage_json} missing; "
            "skipping (gcovr not run)"
        )
        return 0

    # Summary JSON doesn't carry per-line counts.  We need the detail
    # variant.  Try to read it; if it's the summary form, regenerate.
    data = json.loads(args.coverage_json.read_text(encoding="utf-8"))
    needs_detail = (
        data.get("files")
        and "lines" not in (data["files"][0] if data["files"] else {})
    )
    if needs_detail:
        # Run gcovr with --json (not --json-summary) to get per-line.
        import subprocess
        detail_path = args.coverage_json.with_name("coverage_detail.json")
        gcovr_cmd = [
            "gcovr",
            "--root", str(REPO_ROOT / "recovery"),
            "--filter", "src/lcsas-restore/.*",
            "--exclude", "vendored/.*",
            "--gcov-object-directory",
            str(REPO_ROOT / "recovery" / "build"),
        ]
        # The fault-inject sweep accumulates large gcov counters that trip
        # gcovr's "suspicious hits" parser, so we raise the threshold — but
        # --gcov-suspicious-hits-threshold was renamed/dropped across gcovr
        # versions and newer gcovr (e.g. CI) rejects it as unrecognized.
        # Pass it only when the installed gcovr advertises it (kept in sync
        # with the Makefile's GCOVR_SUSPICIOUS).
        help_out = subprocess.run(
            ["gcovr", "--help"], capture_output=True, text=True,
        ).stdout
        if "gcov-suspicious-hits-threshold" in help_out:
            gcovr_cmd += [
                "--gcov-suspicious-hits-threshold", "999999999999999",
            ]
        gcovr_cmd += ["--json", str(detail_path), str(REPO_ROOT / "recovery")]
        rc = subprocess.run(gcovr_cmd, capture_output=True, text=True)
        if rc.returncode != 0:
            print(f"[exemptions_check] gcovr detail failed:\n{rc.stderr}",
                  file=sys.stderr)
            return 1
        args.coverage_json = detail_path

    exempt = parse_exemptions(args.exemptions_md)
    uncov = parse_uncov(args.coverage_json)

    missing_from_doc = uncov - exempt   # uncov but not documented
    covered_but_listed = exempt - uncov  # listed but now covered
    matched = uncov & exempt

    errors = 0
    if missing_from_doc:
        print(
            f"[exemptions_check] FAIL: {len(missing_from_doc)} uncov line(s) "
            "not listed in EXEMPTIONS.md — add a test or document why:",
            file=sys.stderr,
        )
        for f, ln in sorted(missing_from_doc):
            print(f"  {f}:{ln}", file=sys.stderr)
        errors += 1

    if covered_but_listed:
        print(
            f"[exemptions_check] FAIL: {len(covered_but_listed)} EXEMPTIONS "
            "entry(ies) refer to lines that ARE now covered — remove them:",
            file=sys.stderr,
        )
        for f, ln in sorted(covered_but_listed):
            print(f"  {f}:{ln}", file=sys.stderr)
        errors += 1

    if errors:
        return 1

    print(
        f"[exemptions_check] PASS — {len(matched)} lines exempt, "
        f"all accounted for"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
