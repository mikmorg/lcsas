"""#958: a disc catalog carries no path of the computer that staged it,
and the config owns a mirror's path.

Owner decision (2026-11-23): ``repositories.mirror_path``,
``burn_sessions.staging_dir`` and ``session_volumes.iso_path`` are blank
in every disc copy of the catalog, and the old strings are not left in
the file's free pages.  The master keeps them.  The real-tool journey
(move a mirror, rebuild a master from such discs) is
``tests/integration/test_catalog_paths.py``.
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from lcsas.config.mirrors import MirrorPathError, config_for_repo, mirror_path
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.models import Repository
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import add_session_volume, create_session, recorded_path
from lcsas.db.volumes import create_volume
from lcsas.staging.metadata import DISC_BLANKED_PATH_COLUMNS, HolographicInjector

MARK = "/srv/zz-private-958"


def _master(tmp_path: Path) -> Path:
    """A WAL master whose three path columns carry MARK, and whose free
    pages carry an older mirror path (a scan that refreshed it)."""
    db = tmp_path / "master" / "catalog.db"
    db.parent.mkdir()
    conn = get_connection(db)
    conn.execute("PRAGMA secure_delete=OFF")
    create_all(conn)
    register_repo(conn, "r1", "family", MARK + "-old-mirror/" + "x" * 3000)
    conn.execute("UPDATE repositories SET mirror_path = ?",
                 (MARK + "-mirror",))
    sid = create_session(conn, "TEST_TINY", MARK + "-staging").session_id
    vol = create_volume(conn, "LCSAS_TEST_TINY_2026_0001", "u1", "TEST_TINY",
                        1000)
    add_session_volume(conn, sid, vol.volume_id, MARK + "-staging/x.iso",
                       "", 0)
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return db


def test_blanked_path_columns() -> None:
    assert DISC_BLANKED_PATH_COLUMNS == (
        ("repositories", "mirror_path"),
        ("burn_sessions", "staging_dir"),
        ("session_volumes", "iso_path"),
    )


def test_disc_catalog_blanks_paths(tmp_path: Path) -> None:
    master = _master(tmp_path)
    assert MARK.encode() + b"-old-mirror" in master.read_bytes()

    disc = tmp_path / "disc"
    disc.mkdir()
    HolographicInjector(disc).inject_catalog(master)
    dst = disc / "catalog.db"
    conn = sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
    try:
        for table, column in DISC_BLANKED_PATH_COLUMNS:
            rows = conn.execute(f"SELECT {column} FROM {table}").fetchall()
            assert rows and {r[0] for r in rows} == {""}, table
    finally:
        conn.close()
    assert MARK.encode() not in dst.read_bytes()

    # The master keeps them.
    conn = sqlite3.connect(master)
    try:
        assert conn.execute(
            "SELECT mirror_path FROM repositories").fetchone()[0] == (
            MARK + "-mirror")
    finally:
        conn.close()


def test_recorded_path_blank_is_none() -> None:
    """A blank path is "not here", never Path(''), the current folder."""
    assert recorded_path("") is None
    assert recorded_path("/a/b.iso") == Path("/a/b.iso")


def _config(tmp_path: Path, mirror: Path) -> LCSASConfig:
    return LCSASConfig(
        mirror_base_path=tmp_path / "mb",
        staging_path=tmp_path / "st",
        db_path=tmp_path / "db.sqlite",
        repositories={"family": RepositoryConfig(
            name="family", mirror_path=mirror)},
    )


def _repo(path: str, name: str = "family") -> Repository:
    return Repository(repo_id="r1", name=name, mirror_path=path,
                      encryption_key_id="", created_at="")


def test_mirror_path_config_first(tmp_path: Path) -> None:
    new = tmp_path / "nas" / "family"
    new.mkdir(parents=True)
    config = _config(tmp_path, new)
    # Moved: the catalog's path is stale, the config's is used.
    assert mirror_path(config, _repo("/gone/family")) == new
    # Rebuilt from discs: no catalog path.
    assert mirror_path(config, _repo("")) == new
    # No config: the catalog's path, or nothing.
    assert mirror_path(None, _repo("/gone/family")) == Path("/gone/family")
    assert mirror_path(None, _repo("")) is None


def test_mirror_path_refuses_naming_both(tmp_path: Path) -> None:
    gone = tmp_path / "nas" / "family"
    old = tmp_path / "old"
    old.mkdir()
    with pytest.raises(MirrorPathError) as exc:
        mirror_path(_config(tmp_path, gone), _repo(str(old)))
    assert str(gone) in str(exc.value) and str(old) in str(exc.value)
    assert "[repos.family] mirror_path" in str(exc.value)
    # Same path in both: no refusal; the caller says the mirror is missing.
    assert mirror_path(_config(tmp_path, gone), _repo(str(gone))) == gone


def test_blank_catalog_path_never_joins_the_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``Path('').resolve()`` is the cwd: run from inside a configured
    mirror, a blank path must not join that mirror's entry."""
    mirror = tmp_path / "other"
    mirror.mkdir()
    config = _config(tmp_path, tmp_path / "family")
    config.repositories["other"] = RepositoryConfig(
        name="other", mirror_path=mirror)
    monkeypatch.chdir(mirror)
    assert os.getcwd() == str(mirror)
    cfg = config_for_repo(config, "family", "")
    assert cfg is not None and cfg.name == "family"
