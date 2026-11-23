"""#963: ``catalog validate`` reads the catalog; ``catalog rebuild`` meets
newer discs, never writes a source, and says what it cannot know.

Real rustic repositories, real ISOs mastered by xorriso during ``lcsas
stage``, and every command through ``lcsas.cli.main.main``.  The disc
trees are the staged trees (``st/<session>/<LABEL>/``), copied before a
test changes them.

A "newer" disc catalog is made the way a later LCSAS would write one: a
higher ``schema_version`` row and a CHECK constraint widened in the
table's own DDL, so the file is a sound SQLite database that holds a
value this build does not know.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
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


def _session_dir(inst: ScratchInstall) -> Path:
    """The staging folder of the newest burn session."""
    (row,) = inst.query(
        "SELECT staging_dir FROM burn_sessions WHERE session_id = ?",
        inst.sessions()[-1])
    return Path(str(row[0]))


def _writable(root: Path) -> None:
    for p in [root, *root.rglob("*")]:
        if not p.is_symlink():
            os.chmod(p, p.stat().st_mode | 0o600 | (0o100 if p.is_dir() else 0))


def _copy_tree(src: Path, dest: Path) -> Path:
    shutil.copytree(src, dest)
    _writable(dest)
    return dest


def _tree_state(folder: Path) -> dict[str, str]:
    """Relative path -> sha256 of every file under *folder* (sidecars too)."""
    return {
        str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(folder.rglob("*")) if p.is_file()
    }


class _Archive:
    """Set ``alpha`` in two burn sessions of one disc each: 0001, 0002."""

    def __init__(self, root: Path) -> None:
        inst = ScratchInstall(root / "install", repos=("alpha",))
        inst.backup("alpha", "first.bin", size=5000)
        inst.add_repos()
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        inst.mark_burned(inst.sessions()[-1])
        inst.backup("alpha", "second.bin", size=5000)
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        inst.mark_burned(inst.sessions()[-1])
        self.inst = inst
        self.labels = inst.labels()
        assert len(self.labels) == 2, self.labels
        self.box = root / "box"
        for label, tree in inst.disc_trees().items():
            _copy_tree(tree, self.box / label)

    def discs(self, dest: Path) -> list[Path]:
        """A fresh, writable copy of every disc tree, oldest first."""
        return [_copy_tree(self.box / label, dest / label)
                for label in self.labels]


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_Archive]:
    root = tmp_path_factory.mktemp("catalog-963")
    yield _Archive(root)
    _writable(root)


def _rebuild(inst: ScratchInstall, output: Path, discs: list[Path],
             caplog: pytest.LogCaptureFixture) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        rc = inst.lcsas("catalog", "rebuild", "--output", str(output),
                        *[str(d) for d in discs])
    return rc, caplog.text


def _validate(inst: ScratchInstall, disc: Path,
              caplog: pytest.LogCaptureFixture) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO):
        rc = inst.lcsas("catalog", "validate", str(disc))
    return rc, caplog.text


def _make_newer(catalog: Path, *, version: int = 11,
                widen: tuple[str, str, str] | None = None) -> None:
    """Make *catalog* look written by a later LCSAS (schema *version*).

    *widen* is ``(table, last_allowed_value, new_value)``: the new value is
    added to that table's CHECK list in its own DDL, as a later
    migration would.
    """
    conn = sqlite3.connect(catalog)
    try:
        conn.execute("INSERT INTO schema_version (version) VALUES (?)",
                     (version,))
        if widen is not None:
            table, last, new = widen
            (sql,) = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            assert f"'{last}'" in sql
            conn.execute("PRAGMA writable_schema = ON")
            conn.execute(
                "UPDATE sqlite_master SET sql = ? WHERE type='table' AND name=?",
                (sql.replace(f"'{last}'", f"'{last}', '{new}'", 1), table))
            (ver,) = conn.execute("PRAGMA schema_version").fetchone()
            conn.execute(f"PRAGMA schema_version = {int(ver) + 1}")
            conn.execute("PRAGMA writable_schema = OFF")
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(catalog)
    try:
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        conn.close()


# --------------------------------------------------------------------------
# catalog validate
# --------------------------------------------------------------------------

def test_validate_passes_on_a_staged_disc(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    disc = archive.discs(tmp_path)[-1]
    rc, log = _validate(archive.inst, disc, caplog)
    assert rc == 0, log
    assert "PASSED" in log


@pytest.mark.parametrize("damage", ["zero-length", "truncated", "wrong-magic"])
def test_validate_fails_on_a_catalog_it_cannot_read(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    damage: str,
) -> None:
    disc = archive.discs(tmp_path)[-1]
    catalog = disc / "catalog.db"
    data = catalog.read_bytes()
    if damage == "zero-length":
        catalog.write_bytes(b"")
    elif damage == "truncated":
        catalog.write_bytes(data[: len(data) // 2])
    else:
        catalog.write_bytes(b"this is no sqlite file" + data[22:])
    rc, log = _validate(archive.inst, disc, caplog)
    assert rc == 1, log
    assert "PASSED" not in log
    assert "catalog" in log.lower()


def test_validate_fails_when_the_catalog_disagrees_with_data(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The catalog's volume_packs for this disc's volume lost a row: the
    manifest and ``data/`` still agree, the catalog does not."""
    disc = archive.discs(tmp_path)[-1]
    conn = sqlite3.connect(disc / "catalog.db")
    try:
        cur = conn.execute(
            """DELETE FROM volume_packs WHERE rowid = (
                   SELECT vp.rowid FROM volume_packs vp
                   JOIN volumes v ON v.volume_id = vp.volume_id
                   WHERE v.label = ? LIMIT 1)""",
            (disc.name,))
        assert cur.rowcount == 1
        conn.commit()
    finally:
        conn.close()
    rc, log = _validate(archive.inst, disc, caplog)
    assert rc == 1, log
    assert "PASSED" not in log


# --------------------------------------------------------------------------
# catalog rebuild: newer discs
# --------------------------------------------------------------------------

def test_rebuild_refuses_a_newer_disc_with_an_unknown_status(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    discs = archive.discs(tmp_path / "box")
    newest = discs[-1]
    _make_newer(newest / "catalog.db", widen=("volumes", "DESTROYED", "LOST"))
    conn = sqlite3.connect(newest / "catalog.db")
    conn.execute("UPDATE volumes SET status = 'LOST' WHERE label = ?",
                 (archive.labels[0],))
    conn.commit()
    conn.close()

    rc, log = _rebuild(archive.inst, tmp_path / "r.db", discs, caplog)

    assert rc == 1, log
    lines = [ln for ln in log.splitlines() if newest.name in ln
             and "newer LCSAS" in ln]
    assert lines, log
    line = lines[-1]
    assert "schema v11" in line
    assert "LOST" in line and "volumes" in line
    assert "damaged" not in line


def test_rebuild_refuses_a_newer_disc_with_an_unknown_event_type(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    discs = archive.discs(tmp_path / "box")
    newest = discs[-1]
    _make_newer(newest / "catalog.db",
                widen=("volume_events", "BURN_RECEIPT_IMPORTED", "FUTURE_EVENT"))
    conn = sqlite3.connect(newest / "catalog.db")
    conn.execute(
        "INSERT INTO volume_events (volume_id, event_type, detail) "
        "SELECT volume_id, 'FUTURE_EVENT', 'later' FROM volumes LIMIT 1")
    conn.commit()
    conn.close()

    rc, log = _rebuild(archive.inst, tmp_path / "r.db", discs, caplog)

    assert rc == 1, log
    lines = [ln for ln in log.splitlines() if newest.name in ln
             and "newer LCSAS" in ln]
    assert lines, log
    line = lines[-1]
    assert "schema v11" in line and "FUTURE_EVENT" in line
    assert "damaged" not in log


def test_rebuild_merges_a_newer_disc_whose_values_fit(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    discs = archive.discs(tmp_path / "box")
    _make_newer(discs[-1] / "catalog.db")
    out = tmp_path / "r.db"

    rc, log = _rebuild(archive.inst, out, discs, caplog)

    assert rc == 0, log
    conn = sqlite3.connect(out)
    labels = {r[0] for r in conn.execute("SELECT label FROM volumes")}
    conn.close()
    assert labels == set(archive.labels)


# --------------------------------------------------------------------------
# catalog rebuild: sources are never written
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sidecar", ["wal", "journal"])
def test_rebuild_leaves_a_source_with_sidecars_byte_identical(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
    sidecar: str,
) -> None:
    """A catalog copied while open leaves ``-wal``/``-shm`` or a hot
    ``-journal`` beside it.  The rebuild must not replay, checkpoint or
    delete them, nor make new files."""
    discs = archive.discs(tmp_path / "box")
    newest = discs[-1]
    work = tmp_path / "work"
    work.mkdir()
    live = work / "catalog.db"
    shutil.copyfile(newest / "catalog.db", live)
    conn = sqlite3.connect(live, isolation_level=None)
    if sidecar == "wal":
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("UPDATE volumes SET used_bytes = used_bytes + 1")
    else:
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA cache_size=1")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("UPDATE volumes SET used_bytes = used_bytes + 1")
        conn.execute("UPDATE packs SET size_bytes = size_bytes")
    # Copy the open catalog with its sidecars, as a careless copy would.
    names = [p.name for p in work.iterdir()]
    assert any(n != "catalog.db" for n in names), names
    for name in names:
        shutil.copyfile(work / name, newest / name)
    if sidecar == "journal":
        conn.execute("ROLLBACK")
    conn.close()
    before = _tree_state(tmp_path / "box")

    rc, log = _rebuild(archive.inst, tmp_path / "r.db", discs, caplog)

    assert rc == 0, log
    assert _tree_state(tmp_path / "box") == before


# --------------------------------------------------------------------------
# catalog rebuild: what it cannot know
# --------------------------------------------------------------------------

def test_rebuild_says_as_of_when_and_what_to_record_again(
    archive: _Archive, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    discs = archive.discs(tmp_path / "box")
    rc, log = _rebuild(archive.inst, tmp_path / "r.db", discs, caplog)
    assert rc == 0, log
    text = " ".join(log.split())
    m = re.search(r"This catalog is as of (\d{4}-\d\d-\d\d), the date of "
                  r"the newest disc read \((\S+)\)", text)
    assert m, log
    assert m.group(2) == archive.labels[-1]
    assert "cannot know what happened after" in text
    for cmd in ("copy destroy", "location move", "verify"):
        assert cmd in text


class _TwiceIssued:
    """Disc 0002 burned, the master lost, rebuilt from disc 0001 only,
    then a new 0002 staged: two discs with one label."""

    def __init__(self, root: Path) -> None:
        inst = ScratchInstall(root / "install", repos=("alpha",))
        inst.backup("alpha", "first.bin", size=5000)
        inst.add_repos()
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        inst.mark_burned(inst.sessions()[-1])
        inst.backup("alpha", "second.bin", size=5000)
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        inst.mark_burned(inst.sessions()[-1])
        first, second = inst.labels()
        trees = inst.disc_trees()
        self.box = root / "box"
        self.disc1 = _copy_tree(trees[first], self.box / "disc1")
        self.old2 = _copy_tree(trees[second], self.box / "old2")
        # The master is lost; it is rebuilt from disc 0001 alone.
        rebuilt = root / "rebuilt.db"
        assert inst.lcsas("catalog", "rebuild", "--output", str(rebuilt),
                          str(self.disc1)) == 0
        for p in inst.db.parent.glob(inst.db.name + "*"):
            if p.suffix != ".lock":
                p.unlink()
        shutil.move(rebuilt, inst.db)
        inst.backup("alpha", "third.bin", size=5000)
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        session = _session_dir(inst)
        (new_tree,) = [t for t in session.iterdir()
                       if t.is_dir() and t.name.startswith("LCSAS_")]
        assert new_tree.name == second, (new_tree.name, second)
        self.new2 = _copy_tree(new_tree, self.box / "new2")
        self.label = second
        self.inst = inst


@pytest.fixture(scope="module")
def twice(tmp_path_factory: pytest.TempPathFactory) -> Iterator[_TwiceIssued]:
    root = tmp_path_factory.mktemp("catalog-963-label")
    yield _TwiceIssued(root)
    _writable(root)


def test_rebuild_stops_on_one_label_with_two_uuids(
    twice: _TwiceIssued, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    out = tmp_path / "r.db"
    rc, log = _rebuild(twice.inst, out, [twice.disc1, twice.old2, twice.new2],
                       caplog)
    assert rc == 1, log
    lines = [ln for ln in log.splitlines() if twice.label in ln
             and str(twice.old2) in ln and str(twice.new2) in ln]
    assert lines, log
    # Nothing was merged: no catalog where one of the two silently won.
    if out.exists():
        conn = sqlite3.connect(out)
        n = conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
        conn.close()
        assert n == 0


# --------------------------------------------------------------------------
# the split record lost by a rebuild (L8)
# --------------------------------------------------------------------------

def _card_split_id(card: Path) -> int:
    m = re.search(r"Split ID\s*:\s*(\d+)", card.read_text(encoding="utf-8"))
    assert m, card
    return int(m.group(1))


def test_split_recorded_again_from_a_card_after_a_rebuild(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The split was made after the newest disc was staged, so no disc
    carries its record.  After a rebuild ``stage`` refuses and says how to
    record the existing split again; recording it from one card lets
    ``stage`` go on, and the new disc names the cards' split."""
    try:
        inst = ScratchInstall(tmp_path / "install", repos=("alpha",))
        inst.backup("alpha", "first.bin", size=5000)
        inst.add_repos()
        assert inst.lcsas("scan") == 0
        assert inst.lcsas("stage") == 0
        inst.mark_burned(inst.sessions()[-1])
        (tree,) = inst.disc_trees().values()
        disc1 = _copy_tree(tree, tmp_path / "box" / tree.name)

        cards = tmp_path / "cards"
        assert inst.lcsas("password", "split", "--repo", "alpha",
                          "-k", "2", "-n", "3", "--out", str(cards)) == 0
        card = cards / "alpha-share-1-card.txt"
        split_id = _card_split_id(card)
        config_text = inst.config.read_text()
        inst.config.write_text(
            config_text + "split = true\nsplit_threshold = 2\nsplit_shares = 3\n")

        # The master is lost and rebuilt from the only disc.
        rebuilt = tmp_path / "rebuilt.db"
        rc, log = _rebuild(inst, rebuilt, [disc1], caplog)
        assert rc == 0, log
        assert "--record-existing" in log, log
        for p in inst.db.parent.glob(inst.db.name + "*"):
            if p.suffix != ".lock":
                p.unlink()
        shutil.move(rebuilt, inst.db)

        inst.backup("alpha", "second.bin", size=5000)
        assert inst.lcsas("scan") == 0
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert inst.lcsas("stage") != 0
        assert "--record-existing" in caplog.text, caplog.text

        # A card of another K/N than the config says is refused.
        inst.config.write_text(
            config_text + "split = true\nsplit_threshold = 2\nsplit_shares = 4\n")
        assert inst.lcsas("password", "split", "--repo", "alpha",
                          "--record-existing", "--share-file", str(card)) == 1
        assert not inst.query("SELECT * FROM key_escrow")
        inst.config.write_text(
            config_text + "split = true\nsplit_threshold = 2\nsplit_shares = 3\n")

        # A card that names another backup set is refused.
        other = tmp_path / "other-card.txt"
        other.write_text(re.sub(r"(Backup set\s*:\s*)alpha", r"\1beta",
                                card.read_text(encoding="utf-8")))
        assert inst.lcsas("password", "split", "--repo", "alpha",
                          "--record-existing", "--share-file", str(other)) == 1
        assert not inst.query("SELECT * FROM key_escrow")

        before = sorted(p.name for p in cards.iterdir())
        assert inst.lcsas("password", "split", "--repo", "alpha",
                          "--record-existing", "--share-file", str(card)) == 0
        assert sorted(p.name for p in cards.iterdir()) == before
        (row,) = inst.query("SELECT threshold, shares, slip39_id FROM key_escrow")
        assert tuple(row) == (2, 3, split_id)

        assert inst.lcsas("stage") == 0
        session = _session_dir(inst)
        markers = list(session.rglob("LCSAS_BACKUP_SET.txt"))
        assert markers
        for marker in markers:
            assert f"split_id={split_id:05d}" in marker.read_text(), marker
    finally:
        _writable(tmp_path)
