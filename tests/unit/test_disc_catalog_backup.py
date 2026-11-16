"""#812: the disc catalog is an online backup, not a file copy.

The disc catalog was a byte copy of the master catalog made after an
unchecked ``PRAGMA wal_checkpoint(TRUNCATE)``.  While another process
held a read transaction (a second ``lcsas status``, a backup script),
the checkpoint stopped early and the copy lacked the newest rows: the
volume being staged and its packs.  ``inject_catalog`` now uses SQLite's
online backup of the orchestrator's own connection.
"""

from __future__ import annotations

import sqlite3
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.staging.metadata import HolographicInjector
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")


def _ro(db: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{db}?mode=ro", uri=True)


def _check_disc_copy(root: Path) -> sqlite3.Connection:
    """The disc copy is a lone rollback-journal file with no free pages."""
    dst = root / "catalog.db"
    assert not (root / "catalog.db-wal").exists()
    assert not (root / "catalog.db-shm").exists()
    assert dst.read_bytes()[18:20] == b"\x01\x01"  # not WAL
    ro = _ro(dst)
    assert ro.execute("PRAGMA freelist_count").fetchone()[0] == 0
    return ro


def test_injector_copies_rows_a_reader_keeps_in_the_wal(tmp_path):
    db = tmp_path / "archive.db"
    master = get_connection(db)
    master.execute("CREATE TABLE t (x)")
    master.execute("INSERT INTO t VALUES (1)")
    master.commit()

    reader = sqlite3.connect(db)
    reader.execute("BEGIN")
    assert reader.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 1

    master.execute("INSERT INTO t VALUES (2)")
    master.commit()
    # What the old code did: the checkpoint cannot pass the reader.
    busy = master.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
    assert busy[1] != busy[2]  # frames left in the WAL

    root = tmp_path / "staging"
    root.mkdir()
    HolographicInjector(root).inject_catalog(master)
    ro = _check_disc_copy(root)
    try:
        assert ro.execute("SELECT x FROM t ORDER BY x").fetchall() == [
            (1,), (2,)]
    finally:
        ro.close()
        reader.rollback()
        reader.close()
        master.close()


def test_injector_reads_a_catalog_file_by_path(tmp_path):
    db = tmp_path / "archive.db"
    master = get_connection(db)
    master.execute("CREATE TABLE t (x)")
    master.execute("INSERT INTO t VALUES (7)")
    master.commit()
    root = tmp_path / "staging"
    root.mkdir()
    HolographicInjector(root).inject_catalog(db)
    master.close()
    ro = _check_disc_copy(root)
    try:
        assert ro.execute("SELECT x FROM t").fetchall() == [(7,)]
    finally:
        ro.close()


def test_stage_disc_catalog_has_its_volume_while_a_reader_is_open(
    tmp_path, monkeypatch,
):
    """A second process reading the master catalog during the stage does
    not keep the volume out of its own disc catalog."""
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )
    config = _make_config(tmp_path, num_repos=1)
    conn = get_connection(config.db_path)
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    conn.commit()
    _seed_packs(conn, config, num_packs=3, pack_size=50)
    conn.commit()

    reader = sqlite3.connect(config.db_path)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM packs").fetchone()

    xorriso = MagicMock()

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 2048)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            prompt=NullOperatorPrompt())
    try:
        result = orch.stage()
    finally:
        reader.rollback()
        reader.close()

    [m] = result.manifests
    ro = _check_disc_copy(m.staging_path)
    try:
        labels = [r[0] for r in ro.execute("SELECT label FROM volumes")]
        assert labels == [m.volume_label]
        linked = ro.execute(
            "SELECT COUNT(*) FROM volume_packs WHERE volume_id = ?",
            (m.volume_id,),
        ).fetchone()[0]
        assert linked == 3
    finally:
        ro.close()
        conn.close()
