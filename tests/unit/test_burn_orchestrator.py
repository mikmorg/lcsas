"""Tests for burn/orchestrator.py — the central pipeline."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import get_volume_by_id

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(tmp_path: Path) -> LCSASConfig:
    """Build a minimal config for testing."""
    mirror = tmp_path / "mirror"
    staging = tmp_path / "staging"
    db_path = tmp_path / "archive.db"
    mirror.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)

    # Create a fake mirror repo data dir with pack files
    data_dir = mirror / "family" / "data"
    data_dir.mkdir(parents=True)

    repo_cfg = RepositoryConfig(
        name="family",
        mirror_path=mirror / "family",
    )

    return LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=db_path,
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=1000,
        label_prefix="TEST",
        repositories={"family": repo_cfg},
    )


def _seed_db_and_mirror(conn, config: LCSASConfig, num_packs: int = 5):
    """Register a repo and packs in the DB, and create matching files on disk."""
    # Ensure db_path parent exists so inject_catalog can copy it
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    config.db_path.write_bytes(b"sqlite_placeholder")

    register_repo(conn, "family", "Family Photos", str(config.repositories["family"].mirror_path))
    data_dir = config.repositories["family"].mirror_path / "data"

    # Create packs and mirror files
    packs = []
    for i in range(1, num_packs + 1):
        sha = f"{i:064x}"
        p = register_pack(conn, sha256=sha, size_bytes=100 * i, repo_id="family")
        packs.append(p)
        # Put a file in the mirror's data dir
        pack_file = data_dir / sha
        pack_file.write_bytes(b"d" * (100 * i))

    # Also create metadata dirs for holographic injection
    mirror_path = config.repositories["family"].mirror_path
    for subdir in ["index", "snapshots", "keys"]:
        (mirror_path / subdir).mkdir(exist_ok=True)
    (mirror_path / "config").write_text('{"version": 2}')

    return packs


@pytest.fixture
def orch_env(tmp_path):
    """Create a complete orchestrator test environment."""
    config = _make_config(tmp_path)
    conn = get_memory_connection()
    create_all(conn)
    packs = _seed_db_and_mirror(conn, config)

    xorriso = MagicMock()
    dvdisaster = MagicMock()

    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster)
    return {
        "orch": orch,
        "config": config,
        "conn": conn,
        "packs": packs,
        "xorriso": xorriso,
        "dvdisaster": dvdisaster,
    }


# =========================================================================
# BurnOrchestrator.prepare()
# =========================================================================


class TestPrepare:
    def test_prepare_happy_path(self, orch_env):
        """Prepare selects packs, creates staging, registers volume."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]

        manifest = orch.prepare()

        assert manifest.volume_label.startswith("TEST_")
        assert manifest.total_data_bytes > 0
        assert len(manifest.selected_packs) > 0
        assert manifest.staging_path.is_dir()
        assert manifest.media_type == MediaType.TEST_TINY

        # Volume should exist in DB with STAGING status
        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

        # Packs should be linked
        linked = get_pack_ids_for_volume(conn, manifest.volume_id)
        assert len(linked) == len(manifest.selected_packs)

    def test_prepare_no_unarchived_raises(self, orch_env):
        """ValueError when nothing to archive."""
        orch = orch_env["orch"]

        # Archive all packs first
        orch.prepare()
        # Now try again — all packs linked to a volume
        with pytest.raises(ValueError, match="No unarchived packs"):
            orch.prepare()

    def test_prepare_packs_too_large_raises(self, tmp_path):
        """ValueError when packs don't fit the media."""
        config = _make_config(tmp_path)
        conn = get_memory_connection()
        create_all(conn)
        register_repo(conn, "family", "Family", str(config.repositories["family"].mirror_path))

        # Register a pack that's larger than TEST_TINY capacity
        huge_size = MediaType.TEST_TINY.capacity_bytes + 1
        register_pack(conn, sha256="huge_pack", size_bytes=huge_size, repo_id="family")

        # Ensure db_path exists for inject_catalog
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")

        orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="No packs fit"):
            orch.prepare()

    def test_prepare_with_repo_filter(self, tmp_path):
        """Filter packs by specific repo_ids."""
        config = _make_config(tmp_path)
        conn = get_memory_connection()
        create_all(conn)
        register_repo(conn, "family", "Family", str(config.repositories["family"].mirror_path))
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        # Create packs in both repos
        data_dir = config.repositories["family"].mirror_path / "data"
        sha_f = "f" * 64
        sha_w = "a" * 64
        register_pack(conn, sha256=sha_f, size_bytes=100, repo_id="family")
        register_pack(conn, sha256=sha_w, size_bytes=200, repo_id="work")
        (data_dir / sha_f).write_bytes(b"x" * 100)

        # Ensure db_path exists for inject_catalog
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")

        orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
        manifest = orch.prepare(repo_ids=["family"])

        assert len(manifest.selected_packs) == 1
        assert manifest.selected_packs[0].sha256 == sha_f

    def test_prepare_volume_label_sequencing(self, orch_env):
        """Subsequent prepares increment the sequence number."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]

        m1 = orch.prepare()
        # Mark volume as verified so packs become "archived"
        from lcsas.db.volumes import update_status
        update_status(conn, m1.volume_id, "VERIFIED")

        # Add more packs
        data_dir = orch_env["config"].repositories["family"].mirror_path / "data"
        for i in range(6, 9):
            sha = f"{i:064x}"
            register_pack(conn, sha256=sha, size_bytes=50, repo_id="family")
            (data_dir / sha).write_bytes(b"d" * 50)

        m2 = orch.prepare()
        # Parse seq numbers from labels
        seq1 = int(m1.volume_label.split("_")[-1])
        seq2 = int(m2.volume_label.split("_")[-1])
        assert seq2 == seq1 + 1

    def test_prepare_staging_dir_has_data(self, orch_env):
        """Staging directory should contain hardlinked pack files."""
        orch = orch_env["orch"]
        manifest = orch.prepare()

        data_dir = manifest.staging_path / "data"
        assert data_dir.is_dir()
        files = list(data_dir.iterdir())
        assert len(files) > 0

    def test_prepare_staging_has_volume_info(self, orch_env):
        """Staging should have volume_info.json after prepare."""
        orch = orch_env["orch"]
        manifest = orch.prepare()

        vi = manifest.staging_path / "volume_info.json"
        assert vi.is_file()

    def test_prepare_staging_has_catalog(self, orch_env):
        """Staging should have catalog.db after prepare."""
        orch = orch_env["orch"]
        config = orch_env["config"]

        # Create the source catalog DB file
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"sqlite3_data")

        manifest = orch.prepare()
        catalog = manifest.staging_path / "catalog.db"
        assert catalog.is_file()


# =========================================================================
# BurnOrchestrator.execute()
# =========================================================================


class TestExecute:
    def _prepare(self, orch_env):
        orch = orch_env["orch"]
        config = orch_env["config"]
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")
        return orch.prepare()

    def test_execute_skip_burn_skip_ecc(self, orch_env):
        """ISO-only path: creates ISO, skips ECC and physical burn."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)

        vol = orch.execute(manifest, skip_burn=True, skip_ecc=True)

        # xorriso.create_iso called, burn_iso NOT called
        xorriso.create_iso.assert_called_once()
        xorriso.burn_iso.assert_not_called()
        dvdisaster.augment_iso.assert_not_called()

        # Volume should be VERIFIED (via status update) and closed
        assert vol.status == "VERIFIED"
        assert vol.closed_at is not None

    def test_execute_with_ecc(self, orch_env):
        """ECC augmentation is called when skip_ecc=False."""
        orch = orch_env["orch"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)

        orch.execute(manifest, skip_burn=True, skip_ecc=False)

        dvdisaster.augment_iso.assert_called_once()

    def test_execute_with_burn(self, orch_env):
        """Physical burn is called when skip_burn=False."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)

        orch.execute(manifest, skip_burn=False, skip_ecc=True)

        xorriso.burn_iso.assert_called_once()

    def test_execute_custom_iso_output(self, orch_env, tmp_path):
        """ISO output path can be overridden."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)
        custom_iso = tmp_path / "custom" / "output.iso"

        orch.execute(manifest, iso_output=custom_iso, skip_burn=True, skip_ecc=True)

        call_args = xorriso.create_iso.call_args
        assert call_args[0][1] == custom_iso

    def test_execute_failure_reverts_status(self, orch_env):
        """If xorriso.create_iso throws, status reverts BURNING → STAGING."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)

        xorriso.create_iso.side_effect = subprocess.CalledProcessError(1, "xorriso")

        with pytest.raises(subprocess.CalledProcessError):
            orch.execute(manifest, skip_burn=True, skip_ecc=True)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_ecc_failure_reverts_status(self, orch_env):
        """If dvdisaster fails, status reverts."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)

        dvdisaster.augment_iso.side_effect = subprocess.CalledProcessError(1, "dvdisaster")

        with pytest.raises(subprocess.CalledProcessError):
            orch.execute(manifest, skip_burn=True, skip_ecc=False)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_burn_failure_reverts_status(self, orch_env):
        """If physical burn fails, status reverts."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)

        xorriso.burn_iso.side_effect = subprocess.CalledProcessError(1, "xorriso")

        with pytest.raises(subprocess.CalledProcessError):
            orch.execute(manifest, skip_burn=False, skip_ecc=True)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_status_transitions(self, orch_env):
        """Status transitions: STAGING → BURNING → VERIFIED."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        manifest = self._prepare(orch_env)

        # Before execute, should be STAGING
        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

        vol = orch.execute(manifest, skip_burn=True, skip_ecc=True)
        assert vol.status == "VERIFIED"


# =========================================================================
# BurnOrchestrator.abort()
# =========================================================================


class TestAbort:
    def test_abort_removes_volume_and_staging(self, orch_env):
        """Abort deletes the DB volume and cleans up staging dir."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        config = orch_env["config"]
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")
        manifest = orch.prepare()

        assert manifest.staging_path.is_dir()
        vol_id = manifest.volume_id

        orch.abort(manifest)

        assert not manifest.staging_path.exists()
        with pytest.raises(ValueError):
            get_volume_by_id(conn, vol_id)

    def test_abort_removes_iso_file(self, orch_env, tmp_path):
        """Abort removes ISO file if it was created."""
        orch = orch_env["orch"]
        config = orch_env["config"]
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")
        manifest = orch.prepare()

        # simulate an ISO file existing
        iso_file = tmp_path / "test.iso"
        iso_file.write_bytes(b"ISO_CONTENT")
        manifest.iso_path = iso_file

        orch.abort(manifest)

        assert not iso_file.exists()

    def test_abort_no_iso_file(self, orch_env):
        """Abort succeeds even if there's no ISO file."""
        orch = orch_env["orch"]
        config = orch_env["config"]
        config.db_path.parent.mkdir(parents=True, exist_ok=True)
        config.db_path.write_bytes(b"x")
        manifest = orch.prepare()
        manifest.iso_path = None

        orch.abort(manifest)
        assert not manifest.staging_path.exists()


# =========================================================================
# BurnOrchestrator._get_mirror_paths()
# =========================================================================


class TestGetMirrorPaths:
    def test_with_configured_repos(self, orch_env):
        orch = orch_env["orch"]
        paths = orch._get_mirror_paths()
        assert "family" in paths
        assert paths["family"] == orch_env["config"].repositories["family"].mirror_path

    def test_fallback_to_mirror_base(self, tmp_path):
        """When no repos configured, falls back to mirror_base_path."""
        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.sqlite",
            repositories={},
        )
        conn = get_memory_connection()
        create_all(conn)
        orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
        paths = orch._get_mirror_paths()
        assert "default" in paths
        assert paths["default"] == tmp_path / "mirror"
