"""#656: ``lcsas stage`` never stages silently past unscanned mirror packs.

A backup that ran after the last ``lcsas scan`` leaves pack files in the
mirror that the catalog does not know.  Stage copies the mirror's
snapshot files onto the disc, so it must first register those packs (the
additive half of a scan), or stop and name ``lcsas scan`` when it cannot
read the whole mirror.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.packs import list_packs, register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_latest_session, get_session_volumes
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.packs.stage_sync import sync_unscanned_packs


def _pack(data_dir: Path, i: int, size: int = 64) -> str:
    content = (f"{i:08x}".encode() * size)[:size]
    sha = hashlib.sha256(content).hexdigest()
    (data_dir / sha[:2]).mkdir(parents=True, exist_ok=True)
    (data_dir / sha[:2] / sha).write_bytes(content)
    return sha


def _setup(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config + catalog with one scanned pack; the mirror holds two more."""
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = tmp_path / "mirror" / "fam"
    data = mirror / "data"
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir(parents=True)
        (mirror / sub / "dummy").write_text("{}")
    (mirror / "config").write_text("{}")
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{tmp_path / 'mirror'}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
label_prefix = "T"
allow_unverified_repo_format = true

[repos.fam]
mirror_path = "{mirror}"
""")
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "fam", "fam", str(mirror))
    # Monday's scan: one pack.
    scanned = _pack(data, 1)
    register_pack(conn, sha256=scanned, size_bytes=64, repo_id="fam")
    conn.commit()
    conn.close()
    # Tuesday's backup, never scanned: two more pack files.
    _pack(data, 2)
    _pack(data, 3)
    return cfg, db, data


def _fake_create_iso(self, source_dir, output_iso, volume_label, **_kw):
    Path(output_iso).write_bytes(b"\x00" * 2048)
    return Path(output_iso)


def test_stage_registers_unscanned_packs_and_stages_them(tmp_path, caplog):
    cfg, db, data = _setup(tmp_path)
    caplog.clear()
    with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
               _fake_create_iso), \
            caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage"])
    assert rc == 0, caplog.text
    assert "registered 2 pack file(s)" in caplog.text

    conn = get_connection(db)
    try:
        packs = list_packs(conn, repo_id="fam", include_pruned=False)
        assert len(packs) == 3
        session = get_latest_session(conn)
        [sv] = get_session_volumes(conn, session.session_id)
        staged = set(get_pack_ids_for_volume(conn, sv.volume_id))
        assert staged == {p.pack_id for p in packs}
    finally:
        conn.close()


def test_stage_dry_run_reports_but_does_not_register(tmp_path, caplog):
    cfg, db, _data = _setup(tmp_path)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage", "--dry-run"])
    assert rc == 0
    assert "2 pack file(s) in the mirror are not in the catalog" in caplog.text
    assert "this plan leaves them out" in caplog.text
    conn = get_connection(db)
    try:
        assert len(list_packs(conn, repo_id="fam")) == 1
    finally:
        conn.close()


def test_stage_stops_naming_scan_when_mirror_unreadable(tmp_path, caplog):
    cfg, db, data = _setup(tmp_path)
    from lcsas.packs.scanner import MirrorScanResult

    def _partial(_mirror):
        return MirrorScanResult(packs={}, errors=[f"{data}/ab: Permission denied"])

    caplog.clear()
    with patch("lcsas.packs.stage_sync.scan_mirror_packs", _partial), \
            caplog.at_level(logging.INFO, logger="lcsas"):
        rc = main(["--config", str(cfg), "stage"])
    assert rc == 1
    assert "lcsas scan" in caplog.text
    assert "Refusing to stage" in caplog.text
    conn = get_connection(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM burn_sessions").fetchone()[0] == 0
    finally:
        conn.close()


def test_sync_reports_packs_missing_from_mirror(tmp_path):
    """Prunes are only reported: marking them is lcsas scan's job (BURN-09)."""
    _cfg, db, data = _setup(tmp_path)
    conn = get_connection(db)
    try:
        register_pack(conn, sha256="f" * 64, size_bytes=10, repo_id="fam")
        result = sync_unscanned_packs(conn)
        assert result.unscanned == {"fam": 2}
        assert result.missing_from_mirror == {"fam": 1}
        assert result.incomplete == {}
        gone = [p for p in list_packs(conn, repo_id="fam") if p.sha256 == "f" * 64]
        assert gone and not gone[0].is_pruned
    finally:
        conn.close()
