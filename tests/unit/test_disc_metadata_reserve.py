"""#811: stage measures the disc metadata before it packs volumes.

Every data disc carries the disc catalog, each repository's index,
snapshot and key files, and the recovery tools.  The fixed
``metadata_reserve_mb`` (100 MB) was never checked against them, and an
archive of a few TB outgrows it.  Stage now keeps
``max(configured, measured + margin)`` free on each volume, prints it,
and stops before any staging work when one disc cannot hold the
metadata and a pack.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator, DiscMetadataTooLargeError
from lcsas.config.media import MediaType
from lcsas.db.connection import get_memory_connection
from lcsas.db.models import Pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volumes import list_volumes
from lcsas.ecc.dvdisaster import (
    MIN_EFFECTIVE_REDUNDANCY_PCT,
    rs03_pad_target_bytes,
    rs03_redundancy_pct,
    rs03_staged_image_bytes,
    smallest_fitting_medium_bytes,
)
from lcsas.iso.xorriso import SubprocessXorrisoRunner
from lcsas.staging.metadata import (
    DiscMetadataSize,
    measure_disc_metadata,
    pack_disc_bytes,
)
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")


def _big_index(config, size: int, files: int = 8) -> None:
    """A synthetic repository index of *size* bytes in *files* files."""
    index = config.repositories["family"].mirror_path / "index"
    for i in range(files):
        (index / f"{i:064x}").write_bytes(bytes([i % 251]) * (size // files))


def _orch(tmp_path, monkeypatch, *, media=MediaType.TEST_TINY,
          reserve=100, xorriso=None, packs=4, pack_size=50):
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )
    config = _make_config(tmp_path, num_repos=1, media=media,
                          metadata_reserve_bytes=reserve)
    conn = get_memory_connection()
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=packs, pack_size=pack_size)
    if xorriso is None:
        xorriso = MagicMock()

        def _create_iso(source_dir, output_iso, volume_label, **kwargs):
            Path(output_iso).write_bytes(b"\x00" * 2048)
            return Path(output_iso)

        xorriso.create_iso.side_effect = _create_iso
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            prompt=NullOperatorPrompt())
    return orch, conn, config


def test_measurement_counts_the_index_catalog_and_tools(tmp_path,
                                                        monkeypatch):
    orch, conn, config = _orch(tmp_path, monkeypatch)
    mirrors = {"family": config.repositories["family"].mirror_path}
    small = measure_disc_metadata(conn, mirrors, pack_count=4,
                                  volume_count=1, with_source=False)
    _big_index(config, 600_000)
    big = measure_disc_metadata(conn, mirrors, pack_count=4,
                                volume_count=1, with_source=False)
    assert big.repositories - small.repositories >= 600_000
    # The tier-3 reader always; the LCSAS source only on production media.
    with_source = measure_disc_metadata(conn, mirrors, pack_count=4,
                                        volume_count=1, with_source=True)
    assert small.tools > 100_000
    assert with_source.tools > small.tools + 100_000
    # The catalog grows with the stage's rows.
    more = measure_disc_metadata(conn, mirrors, pack_count=100_000,
                                 volume_count=1, with_source=False)
    assert more.catalog >= small.catalog + 100_000 * 36
    assert big.reserve == big.total + big.margin > big.total


def test_reserve_is_the_larger_of_configured_and_measured(tmp_path,
                                                          monkeypatch):
    orch, _conn, _config = _orch(tmp_path, monkeypatch, reserve=100)
    packs = get_unarchived_packs(_conn)
    reserve, measured = orch._metadata_reserve(packs, MediaType.TEST_TINY)
    assert reserve == measured.reserve > 100

    orch._config = replace(orch._config,
                           metadata_reserve_bytes=measured.reserve + 12_345)
    reserve, measured = orch._metadata_reserve(packs, MediaType.TEST_TINY)
    assert reserve == measured.reserve + 12_345


def test_stage_prints_the_reserve(tmp_path, monkeypatch, caplog):
    orch, _conn, _config = _orch(tmp_path, monkeypatch)
    with caplog.at_level(logging.INFO):
        orch.stage(dry_run=True)
    lines = [r.getMessage() for r in caplog.records
             if "Disc metadata" in r.getMessage()]
    assert lines and "reserving" in lines[0] and "TEST_TINY" in lines[0]


def test_large_index_moves_packs_to_more_volumes(tmp_path, monkeypatch):
    """The packs are planned around the measured metadata."""
    orch, conn, config = _orch(tmp_path, monkeypatch, packs=6,
                               pack_size=200_000)
    before = len(orch._multi_bin_pack(get_unarchived_packs(conn),
                                      MediaType.TEST_TINY))
    _big_index(config, 700_000)
    plans = orch._multi_bin_pack(get_unarchived_packs(conn),
                                 MediaType.TEST_TINY)
    assert len(plans) > before
    reserve, _m = orch._metadata_reserve(get_unarchived_packs(conn),
                                         MediaType.TEST_TINY)
    for packs, _bytes in plans:
        used = sum(pack_disc_bytes(p.size_bytes) for p in packs)
        assert used + reserve <= MediaType.TEST_TINY.usable_bytes


@pytest.mark.skipif(shutil.which("xorriso") is None,
                    reason="needs xorriso to master real ISOs")
def test_large_index_every_real_iso_fits(tmp_path, monkeypatch):
    """With a synthetic large index and no configured reserve, every ISO
    xorriso masters still fits the medium."""
    orch, conn, config = _orch(
        tmp_path, monkeypatch, reserve=0, packs=8, pack_size=180_000,
        xorriso=SubprocessXorrisoRunner(),
    )
    _big_index(config, 500_000, files=40)
    result = orch.stage()
    assert len(result.iso_paths) >= 2
    for iso in result.iso_paths:
        size = iso.stat().st_size
        assert size <= MediaType.TEST_TINY.usable_bytes, iso
        # The measurement is an upper bound of what xorriso wrote.
        listing = subprocess.run(
            ["xorriso", "-indev", str(iso), "-find", "/", "-type", "f"],
            capture_output=True, text=True, check=True,
        )
        assert "catalog.db" in listing.stdout
    linked = conn.execute("SELECT COUNT(*) FROM volume_packs").fetchone()[0]
    assert linked == 8


def test_production_plan_keeps_its_rs03_rung(tmp_path, monkeypatch):
    """On ECC media every planned volume, with its measured metadata,
    stays within the usable bytes, so RS03 pads it to the medium's own
    rung with at least the minimum redundancy."""
    orch, _conn, _config = _orch(tmp_path, monkeypatch, reserve=0)
    big = DiscMetadataSize(catalog=150_000_000, repositories=50_000_000,
                           tools=1_000_000, texts=100_000,
                           iso_overhead=400_000)
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.measure_disc_metadata",
        lambda *a, **k: big,
    )
    media = MediaType.CD700
    packs = [
        Pack(pack_id=i, sha256=f"{i:064x}", size_bytes=60_000_000,
             repo_id="family", is_pruned=False, created_at="")
        for i in range(1, 21)
    ]
    reserve, _m = orch._metadata_reserve(packs, media)
    assert reserve == big.reserve
    plans = orch._multi_bin_pack(packs, media, reserve)
    assert sum(len(p) for p, _b in plans) == len(packs)
    for vol_packs, _bytes in plans:
        image = reserve + sum(pack_disc_bytes(p.size_bytes) for p in vol_packs)
        assert image <= media.usable_bytes
        # After any #732 padding, the augmented image fits the medium and
        # its parity clears the floor.
        target = rs03_pad_target_bytes(image, media.capacity_bytes) or image
        assert rs03_staged_image_bytes(image, media.capacity_bytes) \
            == smallest_fitting_medium_bytes(target) <= media.capacity_bytes
        assert rs03_redundancy_pct(target) >= MIN_EFFECTIVE_REDUNDANCY_PCT


def test_metadata_too_large_stops_before_staging(tmp_path, monkeypatch):
    orch, conn, config = _orch(tmp_path, monkeypatch)
    _big_index(config, MediaType.TEST_TINY.usable_bytes)
    with pytest.raises(DiscMetadataTooLargeError) as exc:
        orch.stage()
    msg = str(exc.value)
    assert "TEST_TINY" in msg
    reserve, _m = orch._metadata_reserve(get_unarchived_packs(conn),
                                         MediaType.TEST_TINY)
    assert f"{reserve:,}" in msg
    orch._xorriso.create_iso.assert_not_called()
    assert list_volumes(conn) == []
    assert conn.execute("SELECT COUNT(*) FROM burn_sessions").fetchone()[0] == 0
    assert list(config.staging_path.iterdir()) == []
    # A dry run says the same.
    with pytest.raises(DiscMetadataTooLargeError):
        orch.stage(dry_run=True)
