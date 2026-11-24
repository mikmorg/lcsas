"""#982: ``scan``'s join of ``[repos.<key>]`` tables to catalog repositories.

By name (and mirror path) first, then by key id for a table the name did
not place; two candidates are refused, naming both, never guessed.  The
real-binary path (a master rebuilt from discs) is in
``tests/integration/test_catalog_paths.py``; these are the clash cases.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from lcsas.db.connection import get_memory_connection
from lcsas.db.repos import join_config_tables, register_repo
from lcsas.db.schema import create_all

KEY_A = "a" * 64
KEY_B = "b" * 64


def _mirror(root: Path, name: str, *keys: str) -> Path:
    mirror = root / name
    (mirror / "keys").mkdir(parents=True)
    for key in keys:
        (mirror / "keys" / key).write_text("{}")
    return mirror


def _catalog(*repos: tuple[str, str, str]) -> sqlite3.Connection:
    """Rows (repo_id, name, key id), with no mirror path: rebuilt from discs."""
    conn = get_memory_connection()
    create_all(conn)
    for repo_id, name, key_id in repos:
        register_repo(conn, repo_id, name, "", encryption_key_id=key_id)
    return conn


def test_name_first_then_key_id(tmp_path: Path) -> None:
    conn = _catalog(("r1", "Family Photos", KEY_A), ("r2", "docs", KEY_B))
    join = join_config_tables(conn, {
        "alpha": _mirror(tmp_path, "alpha", KEY_A),
        # Named: its keys are never read (they would match r1).
        "docs": _mirror(tmp_path, "docs", KEY_A),
    })
    assert {k: r.repo_id for k, r in join.placed.items()} == {
        "alpha": "r1", "docs": "r2"}
    assert join.by_key_id == {"alpha": KEY_A}
    assert join.refused == {}


def test_key_id_of_a_repository_another_table_names_is_refused(
    tmp_path: Path,
) -> None:
    conn = _catalog(("r1", "photos", KEY_A))
    join = join_config_tables(conn, {
        "photos": _mirror(tmp_path, "photos", KEY_A),
        "copy": _mirror(tmp_path, "copy", KEY_A),
    })
    assert {k: r.repo_id for k, r in join.placed.items()} == {"photos": "r1"}
    assert set(join.refused) == {"copy"}
    assert "'photos'" in join.refused["copy"]
    assert "[repos.photos]" in join.refused["copy"]


def test_two_tables_joined_by_one_key_id_are_both_refused(
    tmp_path: Path,
) -> None:
    conn = _catalog(("r1", "Family Photos", KEY_A))
    join = join_config_tables(conn, {
        "alpha": _mirror(tmp_path, "alpha", KEY_A),
        "beta": _mirror(tmp_path, "beta", KEY_A),
    })
    assert join.placed == {}
    assert set(join.refused) == {"alpha", "beta"}
    assert "[repos.beta]" in join.refused["alpha"]
    assert "[repos.alpha]" in join.refused["beta"]


def test_key_files_of_two_repositories_are_refused(tmp_path: Path) -> None:
    conn = _catalog(("r1", "Family Photos", KEY_A), ("r2", "Old", KEY_B))
    join = join_config_tables(conn, {
        "alpha": _mirror(tmp_path, "alpha", KEY_A, KEY_B),
    })
    assert join.placed == {}
    assert "'Family Photos'" in join.refused["alpha"]
    assert "'Old'" in join.refused["alpha"]


def test_no_key_id_places_nothing(tmp_path: Path) -> None:
    conn = _catalog(("r1", "Family Photos", ""))
    join = join_config_tables(conn, {
        "alpha": _mirror(tmp_path, "alpha", KEY_A),
        "gone": tmp_path / "not-mounted",
    })
    assert join.placed == {} and join.refused == {}
