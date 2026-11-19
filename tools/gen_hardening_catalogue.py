"""Print the catalogue of tests/recovery_hardening/test_*.py as a Markdown table.

Usage:
    python3 tools/gen_hardening_catalogue.py           # print the table to stdout

Each row is a test file plus the first line of its module docstring.  Nothing
is written to disk; the table is deliberately not committed (#894).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Core helpers (accept explicit paths so unit tests can pass temp dirs)
# ---------------------------------------------------------------------------


def extract_description(py_path: Path) -> str:
    """Return the first non-blank line of the module docstring, or '(no description)'."""
    try:
        source = py_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
    except (OSError, SyntaxError):
        return "(no description)"

    docstring = ast.get_docstring(tree)
    if not docstring:
        return "(no description)"

    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped

    return "(no description)"


def build_table(test_dir: Path) -> str:
    """Return the Markdown table string (no trailing newline)."""
    test_files = sorted(test_dir.glob("test_*.py"))
    lines = [
        "| File | Catches |",
        "|------|---------|",
    ]
    for py_path in test_files:
        desc = extract_description(py_path)
        lines.append(f"| `{py_path.name}` | {desc} |")
    return "\n".join(lines)


def run(test_dir: Path) -> int:
    """Print the catalogue table to stdout.  Never writes any file."""
    print(build_table(test_dir))
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    if "-h" in argv or "--help" in argv:
        print(__doc__)
        return 0
    if argv:
        print(
            f"ERROR: unknown argument(s): {' '.join(argv)}\n{__doc__}",
            file=sys.stderr,
        )
        return 2

    repo_root = Path(__file__).parent.parent
    return run(repo_root / "tests" / "recovery_hardening")


if __name__ == "__main__":
    sys.exit(main())
