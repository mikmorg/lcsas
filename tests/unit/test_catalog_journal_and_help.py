"""#968 items 28 and 29.

28. ``burn-iso --help`` said the receipt's label defaults to the ISO's
    parent folder; since #514 the code uses the ISO's volume ID (the label
    ``stage`` mastered it with), and the folder only when it has none.
29. A master in DELETE journal mode was switched to WAL by the first
    open, before the version check refused a newer one.  The version is
    now read first, and a newer catalog keeps its journal mode.

A real catalog file and every command through ``main()``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.cli.parser import build_parser


def _burn_iso_help() -> str:
    parser = build_parser()
    sub = next(a for a in parser._actions  # noqa: SLF001 - argparse has no API
               if a.dest == "command")
    return str(sub.choices["burn-iso"].format_help())  # type: ignore[attr-defined]


def test_burn_iso_label_help_names_the_volume_id() -> None:
    text = " ".join(_burn_iso_help().split())
    assert "Defaults to the ISO's volume ID" in text
    assert "Defaults to the ISO's parent directory name" not in text


def _journal_mode(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0])
    finally:
        conn.close()


@pytest.fixture
def newer_delete_mode_master(tmp_path: Path) -> Path:
    """A master written by a newer LCSAS that keeps DELETE journal mode."""
    db = tmp_path / "catalog.db"
    assert main(["--catalog", str(db), "init"]) == 0
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO schema_version (version) VALUES (99)")
    conn.commit()
    assert conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"
    conn.close()
    assert not Path(f"{db}-wal").exists()
    return db


@pytest.mark.parametrize("argv", [
    ["init"],
    ["status"],
    ["repo", "list"],
    ["volume", "impact", "X"],
    ["pack", "unprune", "ab"],
    ["repo", "add", "--force", "x", "/nowhere/x"],
])
def test_a_newer_master_keeps_its_journal_mode(
    newer_delete_mode_master: Path, argv: list[str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    db = newer_delete_mode_master
    main(["--catalog", str(db), *argv])
    capsys.readouterr()
    assert _journal_mode(db) == "delete"
    assert not Path(f"{db}-wal").exists()


def test_a_newer_master_is_still_refused_for_writing(
    newer_delete_mode_master: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["--catalog", str(newer_delete_mode_master), "pack",
                 "unprune", "ab"]) == 1
    assert "Catalog schema is v99" in capsys.readouterr().out


def test_a_current_master_is_still_put_in_wal_mode(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    assert main(["--catalog", str(db), "init"]) == 0
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.close()
    assert main(["--catalog", str(db), "status"]) == 0
    assert _journal_mode(db) == "wal"
