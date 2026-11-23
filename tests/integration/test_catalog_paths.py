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


def test_rebuilt_master_config_key_differs_from_catalog_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A rebuilt catalog has no mirror path to join a config key to a
    catalog name that differs from it (#687).  scan names the way out
    (rename the config table) instead of 'repo add', which would register
    the backup set a second time; after the rename, scan and stage work.
    """
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    assert inst.lcsas("repo", "add", "Family Photos",
                      str(inst.mirror("alpha"))) == 0
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    inst, _labels = _replacement_master(inst, tmp_path)
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
