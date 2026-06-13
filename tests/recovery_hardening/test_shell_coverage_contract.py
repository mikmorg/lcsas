"""Hardening: the `shell-coverage` Makefile recipe stays a real gate (GATE-09).

`make shell-coverage` is the only line-coverage floor on
`recovery/scripts/restore.sh` — the heir's single entry point.  Three
silent defangings are what GATE-09 fixed and these tests freeze shut:

  1. The documented "Threshold: N%" comment and the `--threshold N` flag
     must agree, so the floor can't drift below what the comment promises
     (the original 90-comment / 60-flag gap).
  2. The trace-generating pytest run must NOT end in `|| true`: a broken
     trace run (collection error, exit-5 "no tests collected") is exactly
     the state the gate exists to catch, so it must fail loud.

Stdlib-only, text-level Makefile parsing — same pattern as the other
doc/workflow parity hardening tests in this directory.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
MAKEFILE = REPO_ROOT / "Makefile"


def _shell_coverage_recipe() -> list[str]:
    """Return the recipe lines (comment + body) of the `shell-coverage`
    target: every line from the rule header up to the next blank line or
    next top-level target, plus the comment block immediately above it.
    """
    lines = MAKEFILE.read_text().splitlines()
    # Locate the rule header `shell-coverage:`.
    for i, line in enumerate(lines):
        if re.match(r"^shell-coverage\s*:", line):
            header = i
            break
    else:
        raise AssertionError("Makefile no longer defines a `shell-coverage` target")

    # Walk backward over the leading comment block (the threshold doc).
    start = header
    while start - 1 >= 0 and lines[start - 1].lstrip().startswith("#"):
        start -= 1

    # Walk forward over the recipe body (tab-indented lines).
    end = header + 1
    while end < len(lines) and (lines[end].startswith("\t") or not lines[end].strip()):
        if not lines[end].strip():
            break
        end += 1
    return lines[start:end]


def _comment_threshold(recipe: list[str]) -> int:
    text = "\n".join(recipe)
    m = re.search(r"Threshold:\s*(\d+)\s*%", text)
    assert m, "shell-coverage comment no longer states `Threshold: N%`"
    return int(m.group(1))


def _flag_threshold(recipe: list[str]) -> int:
    text = "\n".join(recipe)
    m = re.search(r"--threshold\s+(\d+)", text)
    assert m, "shell-coverage recipe no longer passes `--threshold N`"
    return int(m.group(1))


def test_comment_threshold_matches_flag() -> None:
    recipe = _shell_coverage_recipe()
    comment = _comment_threshold(recipe)
    flag = _flag_threshold(recipe)
    assert comment == flag, (
        f"shell-coverage documented `Threshold: {comment}%` but enforces "
        f"`--threshold {flag}` — the comment and the flag have drifted. They "
        f"must state the same number (the measured floor) so the gate cannot "
        f"silently promise more than it enforces."
    )


def test_trace_run_does_not_swallow_failures() -> None:
    recipe = _shell_coverage_recipe()
    body = [ln for ln in recipe if not ln.lstrip().startswith("#")]
    text = "\n".join(body)
    assert "|| true" not in text, (
        "shell-coverage's pytest trace run must not end in `|| true`: a "
        "non-zero exit (collection error, test failure, exit-5 'no tests "
        "collected') has to fail the gate, because a broken trace run is "
        "exactly the state shell-coverage exists to catch."
    )
