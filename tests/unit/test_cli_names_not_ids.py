"""#581 (owner decision Q42): commands take names and labels, not only IDs.

The contract test walks the whole argparse tree.  Any argument whose help or
destination calls for an ID must also accept the name or label the owner
thinks in, unless the object has no name at all (a Rustic snapshot, a burn
session, a pack's SHA-256): those are listed in ``_ID_ONLY`` with the reason.
The behaviour tests drive `repo remove/retire/activate` by name and
`consolidate` by label.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.repos import list_repos

# (command path, argument) -> why only an ID exists.
_ID_ONLY = {
    ("lcsas pack unprune", "sha256"): "a pack is named by its SHA-256",
    ("lcsas burn", "--session"): "burn sessions have no name ('latest' works)",
    ("lcsas stage", "--session"): "burn sessions have no name",
    ("lcsas burn-iso", "--session"): "burn sessions have no name",
    ("lcsas session abort", "ref"): "burn sessions have no name ('latest' works)",
    ("lcsas restore plan", "snapshot_id"): "Rustic snapshots have no name",
    ("lcsas restore exec", "snapshot_id"): "Rustic snapshots have no name",
    ("lcsas restore standalone", "--snapshot"): "Rustic snapshots have no name",
    ("lcsas catalog validate", "--content"): "not an identifier: checks pack IDs",
}


def _id_arguments() -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []

    def walk(parser: argparse.ArgumentParser, path: str) -> None:
        for action in parser._actions:
            if isinstance(action, argparse._SubParsersAction):
                for name, sub in action.choices.items():
                    walk(sub, f"{path} {name}")
                continue
            text = f"{action.help or ''} {action.dest or ''}"
            if re.search(r"\bIDs?\b|_ids?\b", text):
                arg = action.option_strings[0] if action.option_strings else action.dest
                found.append((path, arg, action.help or ""))

    walk(build_parser(), "lcsas")
    return found


def test_every_id_argument_also_takes_a_name_or_label() -> None:
    args = _id_arguments()
    assert len(args) >= 10, f"self-check: only {len(args)} ID arguments found"
    bad = [
        f"{path} {arg}: {help_}"
        for path, arg, help_ in args
        if (path, arg) not in _ID_ONLY
        and not re.search(r"\b(name|label)s?\b", help_, re.IGNORECASE)
    ]
    assert not bad, "arguments that take only a catalog ID:\n" + "\n".join(bad)


def test_the_id_only_list_has_no_stale_entries() -> None:
    present = {(path, arg) for path, arg, _ in _id_arguments()}
    assert set(_ID_ONLY) <= present, set(_ID_ONLY) - present


def _catalog(tmp_path: Path) -> tuple[Path, str]:
    db = tmp_path / "test.db"
    assert main(["init", "--db-path", str(db)]) == 0
    assert main(["--db", str(db), "repo", "add", "Family", "/mnt/family"]) == 0
    conn = get_connection(db)
    repo_id = next(r.repo_id for r in list_repos(conn) if r.name == "Family")
    conn.close()
    return db, repo_id


def _status(db: Path, repo_id: str) -> str:
    conn = get_connection(db)
    row = conn.execute(
        "SELECT status FROM repositories WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    conn.close()
    return row[0]


def test_repo_retire_and_activate_take_the_name(tmp_path: Path) -> None:
    db, repo_id = _catalog(tmp_path)
    assert main(["--db", str(db), "repo", "retire", "Family"]) == 0
    assert _status(db, repo_id) == "retired"
    assert main(["--db", str(db), "repo", "activate", "family"]) == 0  # any case
    assert _status(db, repo_id) == "active"
    assert main(["--db", str(db), "repo", "retire", repo_id]) == 0  # IDs still work
    assert main(["--db", str(db), "repo", "retire", "Nobody"]) == 1


def test_repo_remove_takes_the_name(tmp_path: Path, capsys) -> None:
    db, _ = _catalog(tmp_path)
    assert main(["--db", str(db), "repo", "remove", "Family"]) == 0
    assert "Removed repository 'Family'" in capsys.readouterr().out
    conn = get_connection(db)
    assert list_repos(conn) == []
    conn.close()


def test_ambiguous_name_asks_for_the_id(tmp_path: Path, capsys) -> None:
    db, _ = _catalog(tmp_path)
    assert main(["--db", str(db), "repo", "add", "family", "/mnt/other"]) == 0
    # "FAMILY" matches both case-insensitively.
    assert main(["--db", str(db), "repo", "retire", "FAMILY"]) == 1
    assert "matches 2 repositories" in capsys.readouterr().out


def test_consolidate_takes_labels(tmp_path: Path, capsys) -> None:
    from lcsas.db.schema import ensure_schema
    from lcsas.db.volumes import create_volume

    db = tmp_path / "test.db"
    conn = get_connection(db)
    ensure_schema(conn)
    vol = create_volume(conn, "LCSAS_BD25_2026_0001", "u1", "BD25",
                        25_000_000_000, status="VERIFIED")
    conn.commit()
    conn.close()
    # A label resolves; an unknown label is a clean error, not a traceback.
    rc_label = main(["--db", str(db), "consolidate", "LCSAS_BD25_2026_0001"])
    rc_id = main(["--db", str(db), "consolidate", str(vol.volume_id)])
    assert rc_label == rc_id
    assert main(["--db", str(db), "consolidate", "LCSAS_NOPE_0001"]) == 1
    assert "Volume 'LCSAS_NOPE_0001' not found" in capsys.readouterr().out
