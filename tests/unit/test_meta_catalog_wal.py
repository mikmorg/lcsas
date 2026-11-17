"""#861: the meta disc builder reads every committed row of the catalog.

The builder opened the master catalog with ``immutable=1``, which ignores
the write-ahead log: rows committed since the last checkpoint were not
seen, and a new catalog showed no tables at all ("no such table:
repositories").  It now reads through :func:`read_catalog`, a plain
read-only connection that applies the log.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.key_escrow import record_split
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all, ensure_schema
from lcsas.meta.builder import MetaVolumeBuilder, read_catalog
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")


def _wal(db: Path) -> Path:
    return db.with_name(db.name + "-wal")


def _mirror(tmp_path: Path, name: str) -> Path:
    mirror = tmp_path / "mirror" / name
    (mirror / "keys").mkdir(parents=True)
    (mirror / "keys" / "k0").write_text("key-material\n")
    (mirror / "config").write_text("repo-config\n")
    return mirror


def _writer_with_rows_in_the_wal(tmp_path: Path) -> tuple[sqlite3.Connection, Path]:
    """A catalog whose every row, schema included, is still in the WAL.

    The writer stays open: closing the last connection checkpoints.
    """
    db = tmp_path / "catalog.db"
    writer = get_connection(db)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    ensure_schema(writer)
    register_repo(writer, "uuid-photos", "photos",
                  str(_mirror(tmp_path, "photos")))
    record_split(writer, "uuid-photos", 3, 4, 4242)
    writer.commit()
    assert _wal(db).stat().st_size > 0
    # What the old reader saw: the main file has no tables yet.
    old = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        assert old.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE name = 'repositories'"
        ).fetchone()[0] == 0
    finally:
        old.close()
    return writer, db


def _config(tmp_path: Path) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mirror",
        staging_path=tmp_path / "staging",
        db_path=tmp_path / "catalog.db",
        repositories={
            "photos": RepositoryConfig(
                name="photos", mirror_path=tmp_path / "mirror" / "photos",
                password_file=Path("/keys/photos.key"), key_split=True,
                key_threshold=2, key_shares=3,
            ),
        },
    )


def test_meta_build_reads_rows_still_in_the_wal(tmp_path: Path) -> None:
    writer, db = _writer_with_rows_in_the_wal(tmp_path)
    try:
        out = tmp_path / "meta"
        out.mkdir()
        builder = MetaVolumeBuilder(
            out, project_root=tmp_path, config=_config(tmp_path),
            catalog_db_path=db,
        )
        builder._bundle_metadata()
        assert [(r.repo_id, r.bundled) for r in builder.metadata_results] == [
            ("uuid-photos", True)]
        assert (out / "metadata" / "uuid-photos" / "keys" / "k0").is_file()
        # The recorded split (3 of 4), not the config's 2 of 3.
        assert builder._recorded_splits() == {"photos": (3, 4)}
    finally:
        writer.close()


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes any folder")
def test_read_catalog_in_a_read_only_folder_applies_the_wal(
    tmp_path: Path,
) -> None:
    """``mode=ro`` cannot open a WAL catalog in a folder it may not write
    when the ``-shm`` file is missing; the copy it falls back to still
    applies the log."""
    writer, db = _writer_with_rows_in_the_wal(tmp_path)
    frozen = tmp_path / "frozen"
    frozen.mkdir()
    shutil.copyfile(db, frozen / "catalog.db")
    shutil.copyfile(_wal(db), frozen / "catalog.db-wal")
    writer.close()
    frozen.chmod(0o555)
    try:
        with read_catalog(frozen / "catalog.db") as conn:
            assert conn.execute(
                "SELECT repo_id FROM repositories").fetchall() == [
                    ("uuid-photos",)]
        assert sorted(p.name for p in frozen.iterdir()) == [
            "catalog.db", "catalog.db-wal"]
    finally:
        frozen.chmod(0o755)


def test_read_catalog_of_a_closed_catalog(tmp_path: Path) -> None:
    writer, db = _writer_with_rows_in_the_wal(tmp_path)
    writer.close()
    assert not _wal(db).exists()
    with read_catalog(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM key_escrow").fetchone() == (1,)
    with pytest.raises(sqlite3.OperationalError), read_catalog(db) as conn:
        conn.execute("DELETE FROM key_escrow")  # read-only


def test_meta_build_after_a_stage_with_the_catalog_still_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stage leaves its rows in the WAL (no checkpoint after the disc
    catalog injection); a meta build in the same session still sees them.
    The blind fixture builds its meta disc this way."""
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )
    config = _make_config(tmp_path, num_repos=1)
    conn = get_connection(config.db_path)
    conn.execute("PRAGMA wal_autocheckpoint=0")
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    conn.commit()
    _seed_packs(conn, config, num_packs=3, pack_size=50)
    conn.commit()

    xorriso = MagicMock()

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 2048)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            prompt=NullOperatorPrompt())
    try:
        [m] = orch.stage().manifests
        assert _wal(config.db_path).stat().st_size > 0
        with read_catalog(config.db_path) as ro:
            assert ro.execute("SELECT label FROM volumes").fetchall() == [
                (m.volume_label,)]
        out = tmp_path / "meta"
        out.mkdir()
        builder = MetaVolumeBuilder(out, project_root=tmp_path,
                                    catalog_db_path=config.db_path)
        builder._bundle_metadata()
        assert sorted(r.repo_id for r in builder.metadata_results) == sorted(
            config.repositories)
    finally:
        conn.close()
