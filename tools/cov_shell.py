#!/usr/bin/env python3
"""Shell-script line-coverage measurer for restore.sh and friends.

Parses a `bash -x`-style trace and cross-references executed lines
against the source file's *executable* lines (anything not blank,
not pure comment, not a heredoc-body line).  Reports per-file hit
rate + a list of un-hit executable lines.

Trace format (PS4='+ $LINENO '):

    + 12 echo "hello"
    + 13 if [ -z "$VAR" ]; then
    + 15 echo "fallback"

The leading `+` count indicates nesting depth (each level is one `+`).
We strip all leading `+` chars + whitespace, then expect a decimal
line number followed by whitespace + the executed command.

Usage:
    cov_shell.py <trace_file> <source_file> [<source_file>...]
    cov_shell.py --threshold 90 <trace> <source>

Exits non-zero when --threshold is set and coverage < threshold.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Match a bash -x line like '++ 47 some command'.  Captures the line
# number.  We tolerate any number of leading '+' (nested calls).
TRACE_LINE_RE = re.compile(r"^\++\s+(\d+)\s")


def parse_trace(trace_path: Path) -> dict[str, set[int]]:
    """Return {source_file_basename: set_of_executed_line_numbers}.

    bash -x doesn't include the SOURCE FILE in each line by default —
    just the line number.  Callers must pass each source file
    separately.  If a multi-file trace is needed, PS4 must include
    `$BASH_SOURCE` (we don't require that today; restore.sh is one
    file)."""
    hit_lines: set[int] = set()
    for raw in trace_path.read_text(errors="replace").splitlines():
        m = TRACE_LINE_RE.match(raw)
        if m:
            hit_lines.add(int(m.group(1)))
    return {"_default": hit_lines}


def executable_lines(source_path: Path) -> set[int]:
    """Return the set of line numbers in `source_path` that are
    executable shell statements.  We exclude:

      - blank lines
      - lines whose stripped content starts with '#' (pure comments)
      - the shebang
      - lines inside heredocs (`<<EOF` ... `EOF`)
      - lines that are just structural keywords standalone (`fi`,
        `done`, `;;`, `else`, `then`, `do`) — bash -x doesn't trace
        those on their own.

    This is heuristic but tracks what `bash -x` actually emits.
    """
    out: set[int] = set()
    in_heredoc: str | None = None
    in_continuation = False
    heredoc_pat = re.compile(
        r"<<-?\s*['\"]?(?P<tag>[A-Za-z_][A-Za-z0-9_]*)['\"]?"
    )
    structural = {
        "fi", "done", "esac", "}", "{", ";;",
        "else", "then", "do",
    }
    # Structural keywords followed only by fd-redirections (e.g. "} 3>&1",
    # "} 2>&1", "{ 3>&-") are never traced as their own line by bash -x.
    _struct_with_redir = re.compile(
        r"^([{}]|fi|done|esac|;;|else|then|do)\s+[\d<>&|;-]"
    )
    for n, raw in enumerate(source_path.read_text().splitlines(), 1):
        stripped = raw.strip()
        if in_heredoc is not None:
            if stripped == in_heredoc:
                in_heredoc = None
            continue
        # Bash -x does not emit a separate trace event for continuation
        # lines (lines that follow a line ending with \).  Skip them so
        # they don't show up as false "missed" lines in the report.
        # Note: bash traces the FIRST line of a multi-line statement for
        # most constructs (for/while/if headers), so those remain in the
        # coverable set.  Single-statement continuations like multi-line
        # variable assignments are traced at their last line; those are a
        # known minor inaccuracy — the starting line appears "missed" even
        # though the command ran.  Acceptable for our 75%+ gate target.
        is_continuation = in_continuation
        in_continuation = raw.rstrip().endswith("\\")
        if is_continuation:
            continue
        if not stripped:
            continue
        if stripped.startswith("#"):
            continue
        if n == 1 and stripped.startswith("#!"):
            continue
        if stripped in structural or _struct_with_redir.match(stripped):
            continue
        m = heredoc_pat.search(raw)
        if m:
            in_heredoc = m.group("tag")
            # The line that OPENS the heredoc IS executable (the
            # cat/heredoc-feeding command runs).
        out.add(n)
    return out


def report(trace_path: Path, source_paths: list[Path],
           threshold: float | None) -> int:
    traces = parse_trace(trace_path)
    hit = traces.get("_default", set())

    total_exec = 0
    total_hit = 0
    rc = 0

    for src in source_paths:
        exec_lines = executable_lines(src)
        src_hit = exec_lines & hit
        src_miss = exec_lines - hit
        total_exec += len(exec_lines)
        total_hit += len(src_hit)
        pct = 100.0 * len(src_hit) / len(exec_lines) if exec_lines else 100.0
        print(f"\n{src} — {pct:.1f}% "
              f"({len(src_hit)}/{len(exec_lines)} executable lines)")
        if src_miss:
            ranges = _ranges(sorted(src_miss))
            print("  un-hit ranges:")
            for r in ranges:
                print(f"    {r}")

    overall = 100.0 * total_hit / total_exec if total_exec else 100.0
    print(f"\nTOTAL: {overall:.1f}% ({total_hit}/{total_exec})")

    if threshold is not None and overall < threshold:
        print(f"\nFAIL: coverage {overall:.1f}% < threshold {threshold:.1f}%",
              file=sys.stderr)
        rc = 1
    return rc


def _ranges(nums: list[int]) -> list[str]:
    """Collapse consecutive integers into 'a-b' strings."""
    if not nums:
        return []
    out, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(f"{start}" if start == prev else f"{start}-{prev}")
        start = prev = n
    out.append(f"{start}" if start == prev else f"{start}-{prev}")
    return out


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("trace", type=Path)
    p.add_argument("source", type=Path, nargs="+")
    p.add_argument("--threshold", type=float, default=None,
                   help="fail with non-zero exit when coverage < N (percent)")
    args = p.parse_args()

    if not args.trace.exists():
        print(f"trace file not found: {args.trace}", file=sys.stderr)
        return 2
    for s in args.source:
        if not s.exists():
            print(f"source file not found: {s}", file=sys.stderr)
            return 2

    return report(args.trace, args.source, args.threshold)


if __name__ == "__main__":
    sys.exit(main())
