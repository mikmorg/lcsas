"""Generate the ## Catalogue table in tests/recovery_hardening/README.md.

Usage:
    python3 tools/gen_hardening_catalogue.py           # rewrite README.md in-place
    python3 tools/gen_hardening_catalogue.py --check   # exit non-zero if README.md is stale
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


def rewrite_readme(readme_path: Path, table: str) -> bytes:
    """Return the new README.md bytes with the Catalogue table replaced.

    Keeps everything before the table header and everything after the last
    table row unchanged.  Raises ValueError if the Catalogue section is not
    found.
    """
    original = readme_path.read_text(encoding="utf-8")
    lines = original.splitlines(keepends=True)

    # Locate the table header line.
    header_idx: int | None = None
    for i, line in enumerate(lines):
        if line.startswith("| File | Catches |"):
            header_idx = i
            break

    if header_idx is None:
        raise ValueError(
            f"Could not find '| File | Catches |' header in {readme_path}"
        )

    # Skip the separator line immediately after the header.
    sep_idx = header_idx + 1

    # Find the first line after the separator that does NOT start with '|'.
    after_table_idx = sep_idx + 1
    while after_table_idx < len(lines) and lines[after_table_idx].startswith("|"):
        after_table_idx += 1

    # Replace header + separator + rows with fresh table; keep surrounding content.
    prefix_lines = lines[:header_idx]
    suffix_lines = lines[after_table_idx:]

    new_content = (
        "".join(prefix_lines)
        + table
        + "\n"
        + "".join(suffix_lines)
    )

    return new_content.encode("utf-8")


def run(test_dir: Path, readme_path: Path, check_mode: bool) -> int:
    """Core logic.  Returns 0 on success, 1 on stale (check mode)."""
    table = build_table(test_dir)
    new_bytes = rewrite_readme(readme_path, table)

    if check_mode:
        current_bytes = readme_path.read_bytes()
        if current_bytes == new_bytes:
            return 0
        print(
            "ERROR: tests/recovery_hardening/README.md is out of date.\n"
            "Run:  python3 tools/gen_hardening_catalogue.py",
            file=sys.stderr,
        )
        return 1
    else:
        readme_path.write_bytes(new_bytes)
        return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    check_mode = "--check" in argv

    repo_root = Path(__file__).parent.parent
    test_dir = repo_root / "tests" / "recovery_hardening"
    readme_path = test_dir / "README.md"

    return run(test_dir, readme_path, check_mode)


if __name__ == "__main__":
    sys.exit(main())
