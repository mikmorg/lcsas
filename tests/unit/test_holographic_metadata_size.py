"""Regression test: the holographic injection footprint must fit on TEST_TINY.

This is the size-budget guardrail for GH issue #142.  When the
holographic payload (SQLite catalog + standalone restorer + per-repo
Rustic metadata + assorted text files) plus xorriso's ISO 9660 overhead
plus the pack-data budget exceeds the smallest configured media
capacity, the burn pipeline fails with "ISO ... exceeds TEST_TINY
capacity".  That used to be discoverable only by running the e2e test on
a host with ``/mnt/lcsas-data`` mounted (skipped on CI per PR #138), so a
maintainer would see ``make gate`` go red and not know why.

Strategy: reproduce the staging tree that the burn orchestrator produces
for a single TEST_TINY volume — including a realistic pack-data budget —
and assert it fits TEST_TINY both by a conservative pure-Python
projection (sum of bytes + a known ISO 9660 headroom) and, when xorriso
is available, by actually mastering the tree to an ISO.

If this test fails, either:

  * Trim what's bundled (e.g. shrink the standalone restorer, drop a doc
    file),
  * Bump ``MediaType.TEST_TINY`` capacity in ``src/lcsas/config/media.py``
    and update this test's expectation, or
  * Shrink ``MIN_TEST_TINY_PACK_BUDGET`` if the e2e test fixture really
    needs to fit on less.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.models import Volume
from lcsas.db.schema import create_all
from lcsas.staging.metadata import (
    MIN_HOLOGRAPHIC_RESERVE_BYTES,
    HolographicInjector,
)
from lcsas.utils.fs import dir_size_bytes

# Empirically measured: xorriso -as mkisofs -r -J -joliet-long -iso-level 3
# adds roughly 600-700 KB of ISO 9660 overhead on top of a small staging
# tree like the one this test builds.  See PR #142.  If you're staring at
# this constant because the test failed, run xorriso against the fixture
# locally and pick the new value (or shrink the injector).
ISO9660_OVERHEAD_HEADROOM = 700_000

# Minimum pack-data budget a TEST_TINY volume must accept after
# holographic injection.  Sized to bite at the historic 1 MB capacity:
# 300 KB of packs + ~280 KB of holographic payload + 700 KB ISO 9660
# overhead exceeds 1 MB.  This bound also matches the e2e fixture's
# per-volume budget (scripts/e2e_test.py packs ~40 KB but consolidation
# tests and the blind-restore fixture both push closer to ~300 KB per
# volume).  If you shrink this you lose the regression guarantee — the
# point of the test is that the same change which broke #142 would have
# tripped it at commit time.
MIN_TEST_TINY_PACK_BUDGET = 300_000


def _build_representative_staging(
    tmp_path: Path, *, pack_bytes: int = 0,
) -> tuple[Path, Volume]:
    """Build a TEST_TINY-sized staging tree the way the burn pipeline does.

    Two repos (matching the e2e fixture).  When ``pack_bytes > 0`` a
    handful of stub pack files are dropped into ``data/`` so the test can
    measure the staging size with a realistic pack-data load.
    """
    staging_root = tmp_path / "staging" / "TEST_VOL_0001"
    staging_root.mkdir(parents=True)
    (staging_root / "data").mkdir()

    if pack_bytes > 0:
        # Spread the bytes across a few packs in the two-level layout the
        # staging builder uses (data/<prefix>/<hash>).  Use the same per-
        # pack size as the e2e fixture's small files (~10 KB each).
        per_pack = min(10_000, pack_bytes)
        num_packs = max(1, pack_bytes // per_pack)
        for i in range(num_packs):
            sha = f"{i:064x}"
            prefix_dir = staging_root / "data" / sha[:2]
            prefix_dir.mkdir(parents=True, exist_ok=True)
            (prefix_dir / sha).write_bytes(b"\x00" * per_pack)

    # Build two repo mirror trees with realistic-looking metadata.
    # We approximate what `rustic init` + a small backup produces: an
    # index file, a single snapshot, one key, a config blob.  Sizes match
    # what we see on the e2e_test fixture (a few KB each).
    mirror_root = tmp_path / "mirror"
    mirror_paths: dict[str, Path] = {}
    for repo_name in ("family", "work"):
        repo_root = mirror_root / repo_name
        for sub in ("index", "snapshots", "keys", "data"):
            (repo_root / sub).mkdir(parents=True)
        # Rustic index pack — typically 2-4 KB for a small backup.
        (repo_root / "index" / ("0" * 64)).write_bytes(b"\x00" * 4_000)
        # One snapshot record — typically ~1 KB.
        (repo_root / "snapshots" / ("1" * 64)).write_bytes(b"\x00" * 1_200)
        # Encryption key envelope — typically ~500 B.
        (repo_root / "keys" / ("2" * 64)).write_bytes(b"\x00" * 500)
        # Repo config — small JSON, ~150 B in practice.
        (repo_root / "config").write_bytes(b"\x00" * 200)
        mirror_paths[repo_name] = repo_root

    # Build a full SQLite catalog (with schema) for the holographic copy.
    # The schema dominates: empty + schema is ~144 KB on disk because
    # SQLite page-aligns.
    catalog_db = tmp_path / "catalog.db"
    conn = sqlite3.connect(str(catalog_db))
    try:
        create_all(conn)
        conn.commit()
    finally:
        conn.close()

    # Now run the full holographic injection sequence the orchestrator
    # uses on TEST_TINY (write_lcsas_source is gated off for test media —
    # we deliberately match that to keep the budget honest).
    config = LCSASConfig(
        mirror_base_path=mirror_root,
        staging_path=tmp_path / "staging",
        db_path=catalog_db,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        label_prefix="TEST",
        metadata_reserve_bytes=MIN_HOLOGRAPHIC_RESERVE_BYTES,
        repositories={
            name: RepositoryConfig(name=name, mirror_path=path)
            for name, path in mirror_paths.items()
        },
        archive_owner="Test Owner",
        archive_description="regression fixture",
    )

    volume = Volume(
        volume_id=1, label="TEST_VOL_0001", uuid="vol-uuid-0001",
        media_type="TEST_TINY",
        capacity_bytes=MediaType.TEST_TINY.capacity_bytes,
        used_bytes=0, location="Home", status="STAGING",
        created_at="2026-01-01", closed_at=None, verified_at=None,
    )

    injector = HolographicInjector(staging_root)
    injector.inject_metadata(mirror_paths)
    injector.inject_catalog(catalog_db)
    injector.write_volume_info(volume)
    injector.write_restore_instructions()
    injector.write_standalone_restorer()
    # NOTE: write_lcsas_source is skipped by the orchestrator on test
    # media; we mirror that here.
    injector.write_start_here(config)
    injector.write_key_info(config)
    injector.write_config_summary(config)
    injector.write_disc_care()

    return staging_root, volume


def test_holographic_injection_plus_pack_budget_fits_test_tiny(tmp_path):
    """Holographic payload + a realistic pack budget + ISO overhead fit TEST_TINY.

    This is the size-budget guardrail.  Smallest configured media is
    TEST_TINY (used by every test fixture that materializes an ISO
    without burning).  If this test fails, the e2e burn pipeline will
    fail with "ISO exceeds TEST_TINY capacity" on every dev host where
    /mnt/lcsas-data exists.

    We use a pure-Python sum-of-bytes estimate (staging_size +
    ISO9660_OVERHEAD_HEADROOM) rather than calling xorriso here so this
    test runs on the unit-test gate (no external deps).  See
    ``test_holographic_injection_fits_test_tiny_under_xorriso`` for the
    end-to-end check.
    """
    staging_root, _ = _build_representative_staging(
        tmp_path, pack_bytes=MIN_TEST_TINY_PACK_BUDGET,
    )
    staging_size = dir_size_bytes(staging_root)
    estimated_iso = staging_size + ISO9660_OVERHEAD_HEADROOM
    capacity = MediaType.TEST_TINY.capacity_bytes

    assert estimated_iso <= capacity, (
        f"Holographic payload + {MIN_TEST_TINY_PACK_BUDGET:,} B pack "
        f"budget = {staging_size:,} B staging tree, projected to "
        f"{estimated_iso:,} B after ISO 9660 framing "
        f"({ISO9660_OVERHEAD_HEADROOM:,} B headroom).  TEST_TINY "
        f"capacity is {capacity:,} B.  The holographic injection has "
        f"outgrown the smallest configured media.  Either trim what "
        f"HolographicInjector writes, bump MediaType.TEST_TINY, or "
        f"shrink ISO9660_OVERHEAD_HEADROOM if xorriso actually packs "
        f"more densely than 700 KB on this tree."
    )


def test_holographic_reserve_constant_covers_payload(tmp_path):
    """MIN_HOLOGRAPHIC_RESERVE_BYTES must be ≥ the actual staging size.

    The reserve constant is what test fixtures pass to LCSASConfig as
    ``metadata_reserve_bytes`` so bin-packing leaves room for the
    injection.  If the payload exceeds the reserve, bin-packing will let
    pack data overflow the disc and the burn will fail at ISO creation
    rather than at the prepare step where the error is actionable.
    """
    staging_root, _ = _build_representative_staging(tmp_path)
    staging_size = dir_size_bytes(staging_root)

    assert staging_size <= MIN_HOLOGRAPHIC_RESERVE_BYTES, (
        f"Holographic payload is {staging_size:,} bytes but "
        f"MIN_HOLOGRAPHIC_RESERVE_BYTES is "
        f"{MIN_HOLOGRAPHIC_RESERVE_BYTES:,}.  Bump the constant in "
        f"src/lcsas/staging/metadata.py or shrink the injector."
    )


@pytest.mark.skipif(
    shutil.which("xorriso") is None,
    reason="xorriso not installed — sum-of-bytes check still applies",
)
def test_holographic_injection_fits_test_tiny_under_xorriso(tmp_path):
    """End-to-end check: actual ISO from a representative staging tree
    fits TEST_TINY.

    Uses xorriso when available so we catch ISO-framing growth that the
    pure-Python sum-of-bytes test can't see (e.g. a new file added to
    the injector that gets its own ISO block).  Skipped on hosts without
    xorriso; the sum-of-bytes test above is still authoritative on CI.
    """
    staging_root, _ = _build_representative_staging(
        tmp_path, pack_bytes=MIN_TEST_TINY_PACK_BUDGET,
    )
    iso_path = tmp_path / "test_tiny.iso"
    subprocess.run(
        [
            "xorriso", "-as", "mkisofs",
            "-r", "-J", "-joliet-long", "-iso-level", "3",
            "-V", "TEST_VOL_0001",
            "-o", str(iso_path),
            str(staging_root),
        ],
        capture_output=True, check=True, text=True,
    )
    iso_size = iso_path.stat().st_size
    capacity = MediaType.TEST_TINY.capacity_bytes

    assert iso_size <= capacity, (
        f"Representative TEST_TINY ISO is {iso_size:,} bytes, "
        f"exceeding TEST_TINY capacity of {capacity:,} bytes.  This is "
        f"the failure mode of GH issue #142.  Either trim the "
        f"holographic injection, bump MediaType.TEST_TINY, or reduce "
        f"the MIN_TEST_TINY_PACK_BUDGET assumption if e2e_test.py is "
        f"also reduced."
    )
