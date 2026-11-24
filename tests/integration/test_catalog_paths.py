"""#958: the config owns a mirror's path; a disc carries no path of the
computer that made it.

1. The config's ``[repos.<name>] mirror_path`` is where a mirror is.  The
   catalog's ``repositories.mirror_path`` is a record of the last path
   ``scan`` saw.  Moving a mirror takes one config edit: ``scan``,
   ``stage`` and ``burn`` follow it, and ``scan`` refreshes the catalog.
2. A config path that does not exist, with a different path in the
   catalog, is refused naming both, never "Unexpected error".
3. The disc copy of the catalog has ``repositories.mirror_path``,
   ``burn_sessions.staging_dir`` and ``session_volumes.iso_path`` blank,
   and the old strings are nowhere in the file.
4. A master rebuilt from such discs on a replacement computer works with
   a config: ``stage``, ``scan``, ``stage --clean``, ``session abort``,
   ``burn --session`` and ``status`` read a blank path as "not here", and
   never as the current folder.

Real rustic, real xorriso, every owner command through ``main()``.
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

BLANKED = (
    ("repositories", "mirror_path"),
    ("burn_sessions", "staging_dir"),
    ("session_volumes", "iso_path"),
)


def _install(root: Path) -> ScratchInstall:
    """alpha backed up once, its first disc staged and burned."""
    inst = ScratchInstall(root, repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    return inst


def _move_mirror(inst: ScratchInstall, new: Path) -> None:
    """Move alpha's mirror to *new* and point the config at it."""
    old = inst.mirror("alpha")
    new.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(old), str(new))
    inst.config.write_text(inst.config.read_text().replace(
        f'mirror_path = "{old}"', f'mirror_path = "{new}"'))
    inst.mirror = lambda name: new.parent / name  # type: ignore[method-assign]


def _catalog_mirror(db: Path) -> str:
    conn = sqlite3.connect(db)
    try:
        (row,) = conn.execute("SELECT mirror_path FROM repositories").fetchall()
        return str(row[0])
    finally:
        conn.close()


@pytest.fixture
def sentinel_cwd(tmp_path: Path) -> Iterator[Path]:
    """Run from a folder with a file in it: a blank path read as ``Path('')``
    is this folder."""
    here = tmp_path / "cwd"
    here.mkdir()
    (here / "keep.txt").write_text("keep")
    old = Path.cwd()
    os.chdir(here)
    try:
        yield here
    finally:
        os.chdir(old)


def test_moved_mirror_takes_one_config_edit(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x")
    old = inst.mirror("alpha")
    new = tmp_path / "x" / "nas" / "alpha"
    _move_mirror(inst, new)
    inst.backup("alpha", "f2.bin")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("config", "check") == 0
        assert inst.lcsas("scan") == 0
    assert _catalog_mirror(inst.db) == str(new)
    assert str(old) in caplog.text and str(new) in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 0
        assert inst.lcsas("stage") == 0
        assert inst.lcsas("burn", "--session", "latest", "--dry-run") == 0
    assert "Unexpected error" not in caplog.text
    # The new disc carries alpha's keys, from the moved mirror.
    label = inst.labels()[-1]
    keys = (inst.disc_trees()[label] / "metadata" / inst.repo_id("alpha")
            / "keys")
    assert keys.is_dir() and any(keys.iterdir())


def test_moved_mirror_stage_without_scan(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """``stage`` registers the mirror's new packs itself (#656): from the
    config's path, not the catalog's."""
    inst = _install(tmp_path / "x")
    _move_mirror(inst, tmp_path / "x" / "nas" / "alpha")
    inst.backup("alpha", "f2.bin")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    assert "Unexpected error" not in caplog.text
    assert len(inst.labels()) == 2


def test_missing_config_mirror_names_both_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The config names a mirror that is not there, the catalog another
    one: refused, naming both, by the commands that open a mirror."""
    inst = _install(tmp_path / "x")
    old = inst.mirror("alpha")
    gone = tmp_path / "x" / "nas" / "alpha"
    inst.config.write_text(inst.config.read_text().replace(
        f'mirror_path = "{old}"', f'mirror_path = "{gone}"'))

    for argv in (("stage",),
                 ("meta", "build", "--output", str(tmp_path / "meta"),
                  "--allow-no-dvdisaster-source", "--allow-incomplete")):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert inst.lcsas(*argv) == 1, argv
        assert "Unexpected error" not in caplog.text, argv
        assert str(gone) in caplog.text, argv
    # meta build reached the mirror helper: it names the catalog's path.
    assert str(old) in caplog.text
    assert "[repos.alpha] mirror_path" in caplog.text


def test_disc_catalog_carries_no_path(tmp_path: Path) -> None:
    inst = _install(tmp_path / "x")
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    root = str(inst.root).encode()
    for label, tree in inst.disc_trees().items():
        db = tree / "catalog.db"
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            for table, column in BLANKED:
                values = {r[0] for r in conn.execute(
                    f"SELECT {column} FROM {table}")}
                assert values <= {""}, (label, table, values)
            assert conn.execute(
                "SELECT COUNT(*) FROM session_volumes").fetchone()[0]
        finally:
            conn.close()
        assert root not in db.read_bytes(), label
    # The master keeps them.
    assert _catalog_mirror(inst.db) == str(inst.mirror("alpha"))
    # The heir restores from these discs through the real restore.sh.
    run = inst.heir_restore("alpha", tmp_path / "out")
    assert run.returncode == 0, run.stdout + run.stderr
    assert {p.name for p in (tmp_path / "out").rglob("*.bin")} == {
        "f1.bin", "f2.bin"}


def _replacement_master(
    inst: ScratchInstall, tmp_path: Path,
) -> tuple[ScratchInstall, dict[str, str]]:
    """Copy every staged disc, lose the computer, rebuild on a new one.

    The mirror and the staging area move away (the old paths no longer
    exist), the discs' catalogs are blanked as #958's discs are, and a
    catalog rebuilt from them is the new master under a config that
    names the new mirror.  Returns the new install and the session of
    each label.
    """
    discs = tmp_path / "discs"
    labels: dict[str, str] = {}
    for session in inst.staging.iterdir():
        for tree in session.iterdir():
            if tree.is_dir() and tree.name.startswith("LCSAS_"):
                shutil.copytree(tree, discs / tree.name)
                labels[tree.name] = session.name
    for disc in discs.iterdir():
        conn = sqlite3.connect(disc / "catalog.db")
        for table, column in BLANKED:
            conn.execute(f"UPDATE {table} SET {column} = ''")
        conn.commit()
        conn.close()

    new_root = tmp_path / "new"
    _move_mirror(inst, new_root / "repos" / "alpha")
    shutil.rmtree(inst.staging)
    shutil.rmtree(inst.db.parent)
    for sub in ("db", "st"):
        (new_root / sub).mkdir(parents=True, exist_ok=True)
    text = inst.config.read_text()
    text = text.replace(str(inst.db), str(new_root / "db" / "catalog.db"))
    text = text.replace(f'staging = "{inst.staging}"',
                        f'staging = "{new_root / "st"}"')
    inst.config.write_text(text)
    inst.db = new_root / "db" / "catalog.db"
    inst.staging = new_root / "st"
    assert inst.lcsas("catalog", "rebuild",
                      *sorted(str(d) for d in discs.iterdir()),
                      "--output", str(inst.db)) == 0
    return inst, labels


def test_rebuilt_master_with_blank_paths(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, sentinel_cwd: Path,
) -> None:
    inst = _install(tmp_path / "x")
    s1 = inst.sessions()[-1]
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    s2 = inst.sessions()[-1]
    inst.mark_burned(s2)
    inst.backup("alpha", "f3.bin")
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    s3 = inst.sessions()[-1]

    inst, _labels = _replacement_master(inst, tmp_path)
    for table, column in BLANKED:
        assert {r[0] for r in inst.query(
            f"SELECT {column} FROM {table}")} == {""}, table

    def ok() -> None:
        assert sentinel_cwd.is_dir(), "a blank path removed the cwd"
        assert (sentinel_cwd / "keep.txt").is_file()
        assert "Unexpected error" not in caplog.text
        assert "did not finish" not in caplog.text

    # status: a blank staging folder is not "a stage that did not finish".
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("status") == 0
    ok()

    # The newest session's ISO is not on this computer.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        # /dev/null: CI runners have no optical drive (the CLI checks
        # the device exists before the burn).
        assert inst.lcsas("burn", "--session", s3, "--no-prompt",
                          "--device", "/dev/null") == 1
    ok()
    assert "not on this computer" in caplog.text

    # Sessions whose volumes are verified: clean and abort.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--clean", "--session", s1) == 0
        assert inst.lcsas("session", "abort", s2) == 0
    ok()
    assert {str(r[0]) for r in inst.query(
        "SELECT session_id FROM burn_sessions WHERE status = 'CLEANED'"
    )} == {s1, s2}

    # A new backup: stage with no scan first, then scan.
    inst.backup("alpha", "f4.bin")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 0
    ok()
    assert _catalog_mirror(inst.db) == ""
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert _catalog_mirror(inst.db) == str(inst.mirror("alpha"))
    ok()


def _disc_names(tree: Path, repo_id: str) -> dict[str, object]:
    """What disc *tree* calls the backup set *repo_id*: its catalog name,
    its marker's ``name=`` and the sets PASSWORD_INFO.txt lists."""
    from lcsas.staging.repo_marker import read_repo_marker

    conn = sqlite3.connect(f"file:{tree / 'catalog.db'}?mode=ro", uri=True)
    try:
        (row,) = conn.execute(
            "SELECT name FROM repositories WHERE repo_id = ?", (repo_id,)
        ).fetchall()
    finally:
        conn.close()
    marker = read_repo_marker(tree / "metadata" / repo_id)
    assert marker is not None
    password_info = (tree / "PASSWORD_INFO.txt").read_text()
    return {
        "catalog": row[0],
        "marker": marker["name"],
        "password_info": sorted(
            line.removeprefix("Backup set: ")
            for line in password_info.splitlines()
            if line.startswith("Backup set: ")),
    }


def _aliased_install(root: Path) -> ScratchInstall:
    """``repo add "Family Photos"`` under ``[repos.alpha]`` (#687): one
    disc staged and burned on the first computer."""
    inst = ScratchInstall(root, repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    assert inst.lcsas("repo", "add", "Family Photos",
                      str(inst.mirror("alpha"))) == 0
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    return inst


def test_rebuilt_master_joins_a_renamed_config_key_by_key_id(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """#982: a rebuilt catalog has no mirror path to join ``[repos.alpha]``
    to the catalog repository 'Family Photos'.  scan joins them by the
    key id ``repo add`` recorded, found in the mirror's ``keys/``: no
    rename, no second registration, and the next disc carries the set
    under the same id and the same names as the disc before.
    """
    inst = _aliased_install(tmp_path / "x")
    repo_id = inst.repo_id("Family Photos")
    (first,) = inst.labels()
    before = _disc_names(inst.disc_trees()[first], repo_id)
    assert before["catalog"] == "Family Photos"

    inst, _labels = _replacement_master(inst, tmp_path)
    (row,) = inst.query(
        "SELECT repo_id, mirror_path, encryption_key_id FROM repositories")
    # Joined by key, not by path: the disc copy kept the key id only.
    assert row["repo_id"] == repo_id
    assert row["mirror_path"] == ""
    assert row["encryption_key_id"] in {
        p.name for p in (inst.mirror("alpha") / "keys").iterdir()}
    inst.backup("alpha", "f2.bin")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert "Unexpected error" not in caplog.text
    assert "rename the" not in caplog.text
    assert "repo add" not in caplog.text
    assert "key file" in caplog.text and "'Family Photos'" in caplog.text
    assert [tuple(r) for r in inst.query(
        "SELECT repo_id, name FROM repositories")] == [
            (repo_id, "Family Photos")]
    assert _catalog_mirror(inst.db) == str(inst.mirror("alpha"))
    assert inst.query(
        "SELECT COUNT(*) FROM packs WHERE repo_id = ? AND pack_id NOT IN "
        "(SELECT pack_id FROM volume_packs)", repo_id)[0][0] > 0

    # Every later command finds it under the config key.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--repo", "alpha") == 0
    assert "Unexpected error" not in caplog.text
    new = inst.labels()[-1]
    assert new != first
    tree = inst.disc_trees()[new]
    assert (tree / "metadata" / repo_id / "keys").is_dir()
    assert _disc_names(tree, repo_id) == before


def test_rebuilt_master_refuses_a_key_id_of_two_repositories(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """#982: a mirror whose key file two catalog repositories recorded (a
    copied mirror registered twice) is refused, naming both, never
    guessed; neither repository changes."""
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    copy = tmp_path / "x" / "copy" / "photos"
    shutil.copytree(inst.mirror("alpha"), copy)
    assert inst.lcsas("repo", "add", "Family Photos",
                      str(inst.mirror("alpha"))) == 0
    assert inst.lcsas("repo", "add", "Old Photos", str(copy)) == 0
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    inst, _labels = _replacement_master(inst, tmp_path)
    rows_before = [tuple(r) for r in inst.query(
        "SELECT * FROM repositories ORDER BY name")]
    packs_before = inst.query("SELECT COUNT(*) FROM packs")[0][0]
    inst.backup("alpha", "f2.bin")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 1
    assert "Unexpected error" not in caplog.text
    assert "'Family Photos'" in caplog.text
    assert "'Old Photos'" in caplog.text
    assert "[repos.alpha]" in caplog.text
    assert "rename the" not in caplog.text
    assert [tuple(r) for r in inst.query(
        "SELECT * FROM repositories ORDER BY name")] == rows_before
    assert inst.query("SELECT COUNT(*) FROM packs")[0][0] == packs_before


def test_rebuilt_master_unplaced_table_keeps_the_rename_advice(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """#979, kept for a table neither join places (#982): here the
    catalog records no key id.  scan names the way out (rename the
    config table) instead of 'repo add', which would register the
    backup set a second time; after the rename, scan and stage work.
    """
    inst = _aliased_install(tmp_path / "x")
    inst, _labels = _replacement_master(inst, tmp_path)
    conn = sqlite3.connect(inst.db)
    conn.execute("UPDATE repositories SET encryption_key_id = ''")
    conn.commit()
    conn.close()
    inst.backup("alpha", "f2.bin")

    caplog.clear()
    with caplog.at_level(logging.INFO):
        inst.lcsas("scan")
    assert "do not 'repo add'" in caplog.text
    assert "'Family Photos'" in caplog.text

    inst.config.write_text(inst.config.read_text().replace(
        "[repos.alpha]", '[repos."Family Photos"]'))
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
    assert "Unexpected error" not in caplog.text
    assert [str(r[0]) for r in inst.query(
        "SELECT name FROM repositories")] == ["Family Photos"]
    assert _catalog_mirror(inst.db) == str(inst.mirror("alpha"))
