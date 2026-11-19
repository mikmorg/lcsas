"""Only the volume lifecycle writes a volume's status or deletes it (#892).

``lcsas.db.lifecycle.VolumeLifecycle`` takes events (a burn started, a
disc check passed, the last disc was lost) and decides the status change,
so a rule change is made in one module.  This check fails on any SQL in
``src/lcsas`` outside ``db/lifecycle.py`` that writes ``volumes.status``
or deletes a ``volumes`` row, and on any call of ``update_status`` or
``delete_volume`` (the functions the lifecycle replaced).  Schema
migrations (``db/schema.py``) may rewrite the table.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "lcsas"

# path relative to src/lcsas -> why it may write volumes.status
_ALLOWED: dict[str, str] = {
    "db/lifecycle.py": "the volume lifecycle itself",
    "db/schema.py": "schema migrations rewrite the volumes table",
}

# An UPDATE of volumes whose SET list (before any WHERE) assigns status,
# and any DELETE of volumes rows.
_STATUS_SQL = re.compile(
    r"\bUPDATE\s+volumes\s+SET\s+(?:(?!\bWHERE\b).)*?\bstatus\s*="
    r"|\bDELETE\s+FROM\s+volumes\b",
    re.IGNORECASE | re.DOTALL,
)
_CALLS = frozenset({"update_status", "delete_volume",
                    "_update_status", "_delete_volume"})


def _findings(source: str) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(ast.parse(source)):
        # Every string constant, f-string parts included.
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and _STATUS_SQL.search(" ".join(node.value.split()))):
            found.append((node.lineno, "SQL writes volumes.status"))
        elif isinstance(node, ast.Call):
            func = node.func
            name = (func.id if isinstance(func, ast.Name)
                    else func.attr if isinstance(func, ast.Attribute) else "")
            if name in _CALLS:
                found.append((node.lineno, f"calls {name}()"))
    return sorted(found)


def test_matcher_catches_a_status_write() -> None:
    bad = (
        "conn.execute('UPDATE volumes SET status = ? WHERE volume_id = ?')\n"
        "conn.execute('''UPDATE volumes\n"
        "                SET verified_at = ?, status = 'VERIFIED'\n"
        "                WHERE volume_id = ?''')\n"
        "conn.execute(f\"update volumes set status = '{s}'\")\n"
        "conn.execute('DELETE FROM volumes WHERE volume_id = ?')\n"
        "update_status(conn, 1, 'VERIFIED')\n"
        "volumes.delete_volume(conn, 1)\n"
    )
    assert [line for line, _ in _findings(bad)] == [1, 2, 5, 6, 7, 8]
    good = (
        "conn.execute('UPDATE volumes SET closed_at = ? WHERE status = ?')\n"
        "conn.execute('UPDATE volume_copies SET status = ?')\n"
        "conn.execute('DELETE FROM volume_packs WHERE volume_id = ?')\n"
        "conn.execute('SELECT status FROM volumes')\n"
        "update_session_status(conn, sid, 'CLEANED')\n"
        "VolumeLifecycle(conn).burn_started(1)\n"
    )
    assert _findings(good) == []


def test_only_the_lifecycle_writes_volume_status() -> None:
    files = sorted(SRC.rglob("*.py"))
    assert len(files) > 50, f"scanned only {len(files)} files under {SRC}"
    assert (SRC / "db" / "lifecycle.py") in files
    unexpected = []
    for path in files:
        rel = path.relative_to(SRC).as_posix()
        if rel in _ALLOWED:
            continue
        source = path.read_text(encoding="utf-8")
        lines = source.splitlines()
        for line, what in _findings(source):
            unexpected.append(
                f"src/lcsas/{rel}:{line}: {what}: {lines[line - 1].strip()}")
    assert not unexpected, (
        "Change a volume's status or delete a volume through an event of "
        "lcsas.db.lifecycle.VolumeLifecycle (#892), adding the event there "
        "if none fits:\n" + "\n".join(unexpected)
    )


def test_the_lifecycle_still_holds_the_writes() -> None:
    """Guards the guard: if the writes moved out of ``db/lifecycle.py``
    under another name, the check above would pass on nothing."""
    found = _findings((SRC / "db" / "lifecycle.py").read_text(encoding="utf-8"))
    assert any(what == "SQL writes volumes.status" for _, what in found)
    assert any(what == "calls _update_status()" for _, what in found)
    assert any(what == "calls _delete_volume()" for _, what in found)
