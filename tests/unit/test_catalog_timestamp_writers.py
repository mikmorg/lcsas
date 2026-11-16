"""Catalog timestamps are written through ``utc_now()`` (#730, #826).

A direct ``datetime.now(...).isoformat()`` writes a different text form
(microseconds, sometimes no offset), and catalog columns that mix forms
sort and compare wrongly as text.  This check fails on any new direct
``now().isoformat()`` call in ``src/lcsas``.  A use that never reaches the
catalog is listed in ``_ALLOWED`` with its reason, by file and line text
(not line number), so unrelated edits do not break it.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lcsas"

# path relative to src/lcsas -> {stripped source line: reason}
_ALLOWED: dict[str, dict[str, str]] = {
    "utils/timestamps.py": {
        'return datetime.now(UTC).isoformat(timespec="milliseconds")':
            "utc_now() itself",
    },
    "utils/labels.py": {
        'ts = datetime.now(UTC).isoformat(timespec="microseconds")':
            "burn session id text, not a timestamp column",
    },
    "cli/commands/key.py": {
        'datetime.now(UTC).isoformat(timespec="seconds"), record.threshold,':
            "AUDIT log line, not the catalog",
    },
    "cli/commands/burn.py": {
        '"burn_date": datetime.now(UTC).isoformat(),':
            "burn-iso receipt file field; import normalises it with "
            "to_utc_iso and records it verbatim (test_portable_burn_loop)",
    },
    "db/connection.py": {
        '"since": datetime.now().astimezone().isoformat(),':
            "lock-holder stamp in the .lock file, not the catalog",
    },
    "burn/orchestrator.py": {
        '"created_at": datetime.now(UTC).isoformat(),':
            "session.json in the staging folder, not the catalog",
        # Catalog writes left for the worker that owns orchestrator.py in
        # rc10 (#826); remove these entries once they use utc_now().
        "burn_date = datetime.now(UTC).isoformat()":
            "#826 follow-up: disc burn date, owned by another rc10 change",
        "verified_at = datetime.now(UTC).isoformat()":
            "#826 follow-up: verify time, owned by another rc10 change",
    },
}


def _is_now_isoformat(node: ast.AST) -> bool:
    """True for ``<...>.now(...)<...>.isoformat(...)`` call chains."""
    if not (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "isoformat"
    ):
        return False
    inner: ast.AST = node.func.value
    while isinstance(inner, ast.Call | ast.Attribute):
        if (
            isinstance(inner, ast.Call)
            and isinstance(inner.func, ast.Attribute)
            and inner.func.attr in ("now", "utcnow")
        ):
            return True
        inner = inner.func if isinstance(inner, ast.Call) else inner.value
    return False


def _findings(source: str) -> list[tuple[int, str]]:
    lines = source.splitlines()
    return [
        (node.lineno, lines[node.lineno - 1].strip())
        for node in ast.walk(ast.parse(source))
        if _is_now_isoformat(node)
    ]


def test_matcher_catches_a_direct_write() -> None:
    bad = (
        "from datetime import UTC, datetime\n"
        "conn.execute('UPDATE volumes SET closed_at = ?',\n"
        "             (datetime.now(UTC).isoformat(),))\n"
        "x = datetime.datetime.now().astimezone().isoformat()\n"
    )
    assert sorted(line for line, _ in _findings(bad)) == [3, 4]
    good = (
        "from lcsas.utils.timestamps import utc_now\n"
        "when = parse_utc(value).isoformat()\n"
        "now = utc_now()\n"
    )
    assert _findings(good) == []


def test_no_direct_now_isoformat_in_src() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 50, f"scanned only {len(files)} files under {SRC}"
    unexpected = []
    for path in files:
        rel = path.relative_to(SRC).as_posix()
        allowed = _ALLOWED.get(rel, {})
        for line, text in _findings(path.read_text(encoding="utf-8")):
            if text not in allowed:
                unexpected.append(f"src/lcsas/{rel}:{line}: {text}")
    assert not unexpected, (
        "Write catalog timestamps with lcsas.utils.timestamps.utc_now(), "
        "not datetime.now(...).isoformat() (#730, #826). If this value "
        "never reaches the catalog, add it to _ALLOWED with the reason:\n"
        + "\n".join(unexpected)
    )
