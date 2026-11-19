#!/usr/bin/env python3
"""Run folders and scoring for the blind-restore harness (#830, #840).

usage:
  blind_runs.py base
      Print the folder that holds the run folders: $LCSAS_BLIND_TMPDIR,
      else $TMPDIR, else /tmp.  sudo -E drops TMPDIR, so a run started
      through run_variant.sh only sees LCSAS_BLIND_TMPDIR.

  blind_runs.py prune [--days N] [--keep PATH]
      Remove kept run folders (lcsas-blind-run-* and lcsas-blind-variant-*,
      directly in the base folder) last changed more than N days ago
      (default $LCSAS_BLIND_KEEP_DAYS, else 14).  Never removes --keep.
      A folder it cannot remove is reported, not fatal.

  blind_runs.py score RUN_DIR --variant NAME [--verify CMD]
      Score one run: run verify.sh (or CMD) on RUN_DIR itself, never on
      "the newest folder".  Prints verify's output, a SCORE line and a
      RESULT line (one line of JSON: run folder, score, cost, turns, the
      OUTCOME and CONDUCT verdicts), and saves the same JSON as
      RUN_DIR/score.json.
      verify.sh's checks come in two groups (#827): OUTCOME (was every
      file restored intact, from the discs, with the documented tools)
      and CONDUCT (did the agent keep the rules).  A run passes when both
      pass, that is when every check of both groups ran and passed.
      Exit 0: all EXPECTED_CHECKS checks ran and passed (OUTCOME and
      CONDUCT both pass).
      Exit 1: every check ran and at least one failed.
      Exit 2: the run cannot be scored: no run folder, no transcript, or
      verify.sh did not report EXPECTED_CHECKS checks of each group (a
      0/0 score).

Runs remove their folder on success and keep it on failure (run_variant.sh
decides, because only it knows the XFAIL ledger).  Set LCSAS_BLIND_KEEP_RUN=1
to keep a passing run's folder too.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

# run_variant.sh runs this as root: import transcript.py from this folder
# without leaving a root-owned __pycache__ in the checkout.
sys.dont_write_bytecode = True
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import transcript  # noqa: E402

# verify.sh's `check` calls of each group (#827).  A pass needs every one
# of them to have run and passed; a 0/0 or 15/15 run is not a pass.
# Pinned against verify.sh by tests/unit/test_blind_runs.py.
EXPECTED_BY_KIND = {"OUTCOME": 9, "CONDUCT": 7}
KINDS = tuple(EXPECTED_BY_KIND)
EXPECTED_CHECKS = sum(EXPECTED_BY_KIND.values())

RUN_PREFIXES = ("lcsas-blind-run-", "lcsas-blind-variant-")
DEFAULT_KEEP_DAYS = 14.0

_PASS = re.compile(r"^PASS\s")
_FAIL = re.compile(r"^FAIL\s")
# A verdict line: "PASS  [OUTCOME] name".
_VERDICT = re.compile(r"^(?P<verdict>PASS|FAIL)\s+\[(?P<kind>[A-Z]+)\]\s")


def base_dir(env: dict[str, str] | None = None) -> Path:
    env = dict(os.environ) if env is None else env
    for var in ("LCSAS_BLIND_TMPDIR", "TMPDIR"):
        value = env.get(var, "").strip()
        if value:
            return Path(value)
    return Path("/tmp")


def keep_days(env: dict[str, str] | None = None) -> float:
    env = dict(os.environ) if env is None else env
    raw = env.get("LCSAS_BLIND_KEEP_DAYS", "").strip()
    if not raw:
        return DEFAULT_KEEP_DAYS
    try:
        days = float(raw)
    except ValueError:
        raise SystemExit(
            f"LCSAS_BLIND_KEEP_DAYS must be a number of days, got {raw!r}"
        ) from None
    if days < 0:
        raise SystemExit(f"LCSAS_BLIND_KEEP_DAYS must not be negative: {raw}")
    return days


def prune(
    base: Path,
    days: float,
    *,
    keep: Path | None = None,
    now: float | None = None,
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Remove run folders in *base* older than *days*.

    Returns (removed, failed).  Only direct children whose name starts
    with one of RUN_PREFIXES and that are real directories (not
    symlinks) are considered.
    """
    removed: list[Path] = []
    failed: list[tuple[Path, str]] = []
    if not base.is_dir():
        return removed, failed
    cutoff = (time.time() if now is None else now) - days * 86400
    keep_resolved = keep.resolve() if keep is not None else None
    for child in sorted(base.iterdir()):
        if not child.name.startswith(RUN_PREFIXES):
            continue
        try:
            if child.is_symlink() or not child.is_dir():
                continue
            if keep_resolved is not None and child.resolve() == keep_resolved:
                continue
            if child.stat().st_mtime >= cutoff:
                continue
            shutil.rmtree(child)
        except OSError as exc:
            failed.append((child, exc.strerror or str(exc)))
            continue
        removed.append(child)
    return removed, failed


def run_stats(transcript_path: Path) -> dict[str, object]:
    """Cost and turns of a run, from the ``result`` events of its own
    transcript (not a grep over the run folder, which also matches the
    same keys quoted inside tool output).  ``turns`` is the CLI's
    ``num_turns``, the counter ``--max-turns`` limits: the most of any
    result event, because the CLI counts afresh each time it wakes for a
    background task and the last event can say 1 after 30 calls (#889).
    ``cost_usd`` is the last ``total_cost_usd``, a running total;
    ``agent_result`` is the last event's subtype; ``tool_calls`` counts
    the agent's tool_use blocks."""
    stats: dict[str, object] = {"cost_usd": None, "turns": None,
                                "tool_calls": 0, "agent_result": None}
    try:
        t = transcript.load(transcript_path)
    except OSError:
        return stats
    stats["cost_usd"] = t.cost_usd()
    stats["turns"] = t.turns()
    if t.results:
        stats["agent_result"] = t.results[-1].get("subtype")
    stats["tool_calls"] = len(t.calls)
    return stats


def count_checks(verify_output: str) -> tuple[int, int]:
    lines = verify_output.splitlines()
    passed = sum(1 for ln in lines if _PASS.match(ln))
    failed = sum(1 for ln in lines if _FAIL.match(ln))
    return passed, failed


def count_by_kind(
    verify_output: str, kinds: tuple[str, ...] = KINDS,
) -> dict[str, dict[str, int]]:
    """{kind: {"passed": n, "failed": m}} from verify.sh's verdict lines.
    A verdict line with no group, or an unknown one, counts under "?"."""
    counts: dict[str, dict[str, int]] = {}
    for line in verify_output.splitlines():
        if not (_PASS.match(line) or _FAIL.match(line)):
            continue
        m = _VERDICT.match(line)
        kind = m.group("kind") if m and m.group("kind") in kinds else "?"
        verdict = "passed" if line.startswith("PASS") else "failed"
        tally = counts.setdefault(kind, {"passed": 0, "failed": 0})
        tally[verdict] += 1
    return counts


def score(
    run_dir: Path,
    variant: str,
    verify_cmd: list[str],
    *,
    expected_by_kind: dict[str, int] | None = None,
) -> tuple[int, dict[str, object], str]:
    """Score *run_dir*.  Returns (exit code, result, verify output).

    The run passes (exit 0) when OUTCOME and CONDUCT both pass: every
    check of each group ran, none failed, and verify.sh exited 0."""
    by_kind_expected = dict(expected_by_kind or EXPECTED_BY_KIND)
    expected = sum(by_kind_expected.values())
    result: dict[str, object] = {
        "variant": variant, "run_dir": str(run_dir),
        "passed": 0, "failed": 0, "expected": expected,
    }
    for kind in by_kind_expected:
        result[kind.lower()] = None  # "pass" / "fail" once scored
    if not run_dir.is_dir():
        result["error"] = f"run folder {run_dir} does not exist"
        return 2, result, ""
    transcript_path = run_dir / "transcript.jsonl"
    if not transcript_path.is_file():
        result["error"] = f"no transcript in {run_dir}: the agent never ran"
        return 2, result, ""
    result.update(run_stats(transcript_path))
    proc = subprocess.run(
        [*verify_cmd, str(run_dir)],
        capture_output=True, text=True, check=False,
    )
    output = proc.stdout
    if proc.stderr:
        output += proc.stderr
    passed, failed = count_checks(proc.stdout)
    result.update(passed=passed, failed=failed, verify_exit=proc.returncode)
    if passed + failed != expected:
        result["error"] = (
            f"verify.sh reported {passed + failed} checks, expected "
            f"{expected}: the run was not scored"
        )
        return 2, result, output
    by_kind = count_by_kind(proc.stdout, tuple(by_kind_expected))
    for kind, want in by_kind_expected.items():
        tally = by_kind.get(kind, {"passed": 0, "failed": 0})
        result[f"{kind.lower()}_passed"] = tally["passed"]
        result[f"{kind.lower()}_failed"] = tally["failed"]
        if tally["passed"] + tally["failed"] != want:
            result["error"] = (
                f"verify.sh reported {tally['passed'] + tally['failed']} "
                f"{kind} checks, expected {want}: the run was not scored"
            )
            return 2, result, output
        result[kind.lower()] = "pass" if tally["failed"] == 0 else "fail"
    if failed == 0 and proc.returncode == 0:
        return 0, result, output
    if failed == 0:
        result["error"] = f"verify.sh exited {proc.returncode} with no FAIL line"
        return 2, result, output
    return 1, result, output


def _cmd_score(args: argparse.Namespace) -> int:
    verify_cmd = args.verify.split() if args.verify else [str(HERE / "verify.sh")]
    run_dir = Path(args.run_dir)
    code, result, output = score(run_dir, args.variant, verify_cmd)
    if output:
        sys.stdout.write(output if output.endswith("\n") else output + "\n")
    print()
    # The sweep scripts read "SCORE: n/m (variant=...)"; the groups follow.
    groups = "  ".join(
        f"{kind} {str(result.get(kind.lower()) or 'not scored').upper()}"
        for kind in KINDS
    )
    print(f"SCORE: {result['passed']}/{result['expected']} "
          f"(variant={args.variant})  {groups}")
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
    result["score_exit"] = code
    line = json.dumps(result, sort_keys=True)
    print(f"RESULT: {line}")
    if run_dir.is_dir():
        try:
            (run_dir / "score.json").write_text(line + "\n")
        except OSError as exc:
            print(f"WARNING: could not save score.json: {exc.strerror}",
                  file=sys.stderr)
    return code


def _cmd_prune(args: argparse.Namespace) -> int:
    days = args.days if args.days is not None else keep_days()
    base = base_dir()
    removed, failed = prune(
        base, days, keep=Path(args.keep) if args.keep else None,
    )
    for path in removed:
        print(f"pruned old blind run folder {path}")
    for path, why in failed:
        print(f"WARNING: could not prune {path}: {why}", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("base")
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--days", type=float)
    p_prune.add_argument("--keep")
    p_score = sub.add_parser("score")
    p_score.add_argument("run_dir")
    p_score.add_argument("--variant", required=True)
    p_score.add_argument("--verify", help="verify command (default verify.sh)")
    args = ap.parse_args(argv)
    if args.cmd == "base":
        print(base_dir())
        return 0
    if args.cmd == "prune":
        return _cmd_prune(args)
    return _cmd_score(args)


if __name__ == "__main__":
    raise SystemExit(main())
