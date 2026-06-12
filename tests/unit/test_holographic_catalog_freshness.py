"""BURN-08 — pin the on-disc catalog's self-staleness contract.

The holographic catalog is copied into staging BEFORE the ISO is
mastered; burn results (volume_copies rows, VERIFIED statuses) reach
the hot DB only AFTER the burn.  Every disc of session N therefore
carries a catalog in which session N's own volumes are STAGING with
zero copies at zero locations — and the FINAL session's discs are
permanently self-stale: nothing newer ever supersedes them.

These tests document that contract (so any change to the write
ordering shows up here) and pin the on-disc disclosure of it: the
START_HERE.txt staleness NOTE must live in the same staged tree as
the stale catalog.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from tests.unit.test_session_pipeline import (
    _make_config,
    _seed_packs,
    _wire_drive_identity,
    _zeros_device_reader,
)


@pytest.fixture
def staged(tmp_path):
    """Stage a 2-volume session against a FILE-backed hot DB.

    Unlike the in-memory env of test_session_pipeline, the orchestrator
    connection here IS ``config.db_path``, so the catalog.db injected
    into each staged tree contains the session's own volume rows — the
    exact artifact a burned disc carries.
    """
    config = _make_config(tmp_path, num_repos=2)
    conn = get_connection(config.db_path)
    create_all(conn)

    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))

    # 3 packs @ 800KB > 2MB TEST_TINY → exactly 2 volumes.
    _seed_packs(conn, config, num_packs=3, pack_size=800_000)

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_zeros_device_reader)

    result = orch.stage()
    assert len(result.manifests) == 2, "fixture must span two volumes"
    return {"config": config, "conn": conn, "orch": orch, "result": result}


def _disc_catalog(staging_path: Path) -> sqlite3.Connection:
    """Open the catalog.db inside a staged tree, read-only."""
    catalog = staging_path / "catalog.db"
    assert catalog.is_file(), f"no holographic catalog in {staging_path}"
    conn = sqlite3.connect(f"file:{catalog}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestOnDiscCatalogSelfStaleness:
    def test_own_volume_is_staging_with_zero_copies(self, staged):
        """Contract: a disc's catalog predates its own burn.

        Each staged tree's catalog shows its OWN volume as STAGING with
        no volume_copies rows — the disc cannot say where its copies
        are.  This is inherent to the write ordering (the catalog must
        be mastered before the burn); the durable receipts and the
        START_HERE NOTE are the compensations.
        """
        for manifest in staged["result"].manifests:
            disc = _disc_catalog(manifest.staging_path)
            try:
                row = disc.execute(
                    "SELECT volume_id, status FROM volumes WHERE label = ?",
                    (manifest.volume_label,),
                ).fetchone()
                assert row is not None, (
                    f"{manifest.volume_label}: own volume row missing "
                    f"from its holographic catalog"
                )
                assert row["status"] == "STAGING"
                copies = disc.execute(
                    "SELECT COUNT(*) FROM volume_copies WHERE volume_id = ?",
                    (row["volume_id"],),
                ).fetchone()[0]
                assert copies == 0
            finally:
                disc.close()

    def test_staleness_is_frozen_at_master_time(self, staged):
        """Burning never retro-updates the already-mastered catalogs.

        After the burn the HOT DB says VERIFIED with a copy at the
        location — but the staged catalog.db (the bytes on the disc)
        still says STAGING with zero copies.  For the final session
        this gap is permanent; the durable receipts under
        ``<db dir>/receipts/`` are the only surviving record.
        """
        result = staged["result"]
        staged["orch"].burn_session(result.session_id, "Home_Shelf",
                                    skip_burn=True)

        for manifest in result.manifests:
            # Hot DB: burn evidence recorded.
            hot = staged["conn"].execute(
                "SELECT v.status, COUNT(c.volume_id) AS copies "
                "FROM volumes v LEFT JOIN volume_copies c "
                "ON c.volume_id = v.volume_id WHERE v.label = ?",
                (manifest.volume_label,),
            ).fetchone()
            assert hot["status"] == "VERIFIED"
            assert hot["copies"] == 1

            # On-disc catalog: still pre-burn.
            disc = _disc_catalog(manifest.staging_path)
            try:
                row = disc.execute(
                    "SELECT v.status, COUNT(c.volume_id) AS copies "
                    "FROM volumes v LEFT JOIN volume_copies c "
                    "ON c.volume_id = v.volume_id WHERE v.label = ?",
                    (manifest.volume_label,),
                ).fetchone()
                assert row["status"] == "STAGING"
                assert row["copies"] == 0
            finally:
                disc.close()

    def test_newer_disc_catalog_supersedes_older(self, staged):
        """Freshest-catalog selection basis: later discs know more.

        Volume 1's catalog was injected before volume 2 existed, so it
        lacks volume 2's row; volume 2's catalog lists both.  This is
        why recovery prefers the NEWEST disc's catalog — and why the
        final session's discs (which nothing supersedes) need the
        receipt instead.
        """
        first, second = staged["result"].manifests

        disc1 = _disc_catalog(first.staging_path)
        try:
            labels1 = {
                r[0] for r in disc1.execute("SELECT label FROM volumes")
            }
        finally:
            disc1.close()
        assert first.volume_label in labels1
        assert second.volume_label not in labels1

        disc2 = _disc_catalog(second.staging_path)
        try:
            labels2 = {
                r[0] for r in disc2.execute("SELECT label FROM volumes")
            }
        finally:
            disc2.close()
        assert {first.volume_label, second.volume_label} <= labels2


class TestStalenessDisclosure:
    def test_start_here_discloses_catalog_staleness(self, staged):
        """The gap must be disclosed wherever it exists: every staged
        tree's START_HERE.txt carries the staleness NOTE pointing the
        heir at the printed receipt / a newer disc's catalog."""
        for manifest in staged["result"].manifests:
            text = (manifest.staging_path / "START_HERE.txt").read_text(
                encoding="utf-8"
            )
            assert "catalog was written BEFORE this disc" in text
            assert "the catalog on any NEWER disc" in text
            assert "printed receipt" in text
