#!/usr/bin/env python3
"""Skip-rot floor: fail if a junit run reports fewer passed tests than a floor.

CI installs qemu-user-static + wine so the aarch64/armv7/windows
committed-binary hardening tests actually execute (they skip everywhere
but the dev VM otherwise).  A regression that turns real assertions into
silent skips — an uninstalled dep, a broken fixture, a `pytest.skip` that
swallows a whole module — would keep the suite "green" while quietly
eroding coverage.  This guard counts *passed* tests in the junit XML and
fails the build if it drops below an explicit floor.

passed = sum over <testsuite>(tests - failures - errors - skipped).

Re-baseline the floor (the workflow's FLOOR env) from a known-good CI run
minus a small margin; never raise it to chase a flake.
"""

from __future__ import annotations

import argparse
import sys
import xml.etree.ElementTree as ET
from pathlib import Path


def count_passed(junit_path: Path) -> int:
    """Return the number of passed testcases across all suites in a junit file."""
    root = ET.parse(junit_path).getroot()
    # The root may be <testsuites> (multiple suites) or a bare <testsuite>.
    suites = root.iter("testsuite")
    total_passed = 0
    for suite in suites:
        tests = int(suite.get("tests", "0"))
        failures = int(suite.get("failures", "0"))
        errors = int(suite.get("errors", "0"))
        skipped = int(suite.get("skipped", "0"))
        total_passed += tests - failures - errors - skipped
    return total_passed


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("junit", type=Path, help="path to a pytest junit XML file")
    parser.add_argument(
        "--min-passed",
        type=int,
        required=True,
        help="fail if passed test count is below this floor",
    )
    args = parser.parse_args(argv)

    if not args.junit.is_file():
        print(f"ci_min_passed: junit file not found: {args.junit}", file=sys.stderr)
        return 2

    passed = count_passed(args.junit)
    if passed < args.min_passed:
        print(
            f"ci_min_passed: FAIL — passed={passed} < floor={args.min_passed}. "
            f"Skip-rot: a test that used to run is now skipping/erroring, or a "
            f"dep/fixture broke.  Investigate before re-baselining the floor.",
            file=sys.stderr,
        )
        return 1

    print(f"ci_min_passed: OK — passed={passed} >= floor={args.min_passed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
