"""Tests for burn/orchestrator.py — the central pipeline."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import get_volume_by_id, list_volumes, update_status
from lcsas.staging.builder import (
    CorruptPacksError,
    MirrorUnavailableError,
    MissingPacksError,
    StagingBuilder,
)
from tests.unit.test_session_pipeline import _wire_drive_identity

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _pack_bytes(i: int, size: int) -> bytes:
    """Deterministic, per-index-distinct content of exactly *size* bytes.

    Staging hash-verifies pack content against the catalog SHA-256
    (BURN-02), so seeded mirror files must really hash to their names.
    """
    seed = f"{i:08x}".encode()
    return (seed * (size // len(seed) + 1))[:size]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()

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

    # Create packs and mirror files (content must hash to the registered
    # sha — staging verifies it, BURN-02)
    packs = []
    for i in range(1, num_packs + 1):
        content = _pack_bytes(i, 100 * i)
        sha = _sha(content)
        p = register_pack(conn, sha256=sha, size_bytes=100 * i, repo_id="family")
        packs.append(p)
        # Put a file in the mirror's data dir
        pack_file = data_dir / sha
        pack_file.write_bytes(content)

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

    # create_iso writes a real (tiny) file so execute()'s post-burn hash
    # compare has an image to hash (FMA-03); content matches the fake
    # device reader below.
    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).parent.mkdir(parents=True, exist_ok=True)
        Path(output_iso).write_bytes(b"fake-iso-data")
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    dvdisaster = MagicMock()

    # BURN-04: deterministic fake device reader matching the b"fake-iso-data"
    # ISOs written by _fake_create_iso / _create_staged_session (the unit
    # suite must never touch a real optical device).
    def fake_device_reader(device: str, length_bytes: int) -> str:
        return hashlib.sha256(b"fake-iso-data"[:length_bytes]).hexdigest()

    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster,
                            device_reader=fake_device_reader)
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

        # Archive all packs, then mark the volume durable: packs on a
        # STAGING volume stay re-selectable under FMA-01 semantics.
        manifest = orch.prepare()
        update_status(orch_env["conn"], manifest.volume_id, "BURNING")
        update_status(orch_env["conn"], manifest.volume_id, "BURNED")
        # Now try again — all packs linked to a durable volume
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
        with pytest.raises(ValueError, match="exceed.*usable capacity"):
            orch.prepare()

    def test_prepare_with_repo_filter(self, tmp_path):
        """Filter packs by specific repo_ids."""
        config = _make_config(tmp_path)
        conn = get_memory_connection()
        create_all(conn)
        register_repo(conn, "family", "Family", str(config.repositories["family"].mirror_path))
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        # Create packs in both repos
        mirror_path = config.repositories["family"].mirror_path
        data_dir = mirror_path / "data"
        content_f = b"x" * 100
        sha_f = _sha(content_f)
        sha_w = "a" * 64  # never staged (filtered out), no file needed
        register_pack(conn, sha256=sha_f, size_bytes=100, repo_id="family")
        register_pack(conn, sha256=sha_w, size_bytes=200, repo_id="work")
        (data_dir / sha_f).write_bytes(content_f)
        # Minimum holographic metadata for the staged repo (BURN-01:
        # repos with packs on the volume must be self-describing).
        (mirror_path / "keys").mkdir(exist_ok=True)
        (mirror_path / "config").write_text('{"version": 2}')

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
        update_status(conn, m1.volume_id, "VERIFIED", force=True)

        # Add more packs
        data_dir = orch_env["config"].repositories["family"].mirror_path / "data"
        for i in range(6, 9):
            content = _pack_bytes(i, 50)
            sha = _sha(content)
            register_pack(conn, sha256=sha, size_bytes=50, repo_id="family")
            (data_dir / sha).write_bytes(content)

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

    def test_execute_skip_burn(self, orch_env):
        """ISO-only path on TEST_TINY: creates ISO, no physical burn.

        TEST_TINY has 0% ECC overhead so DVDisaster is implicitly skipped
        (see MediaType.ecc_overhead_pct).
        """
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)

        vol = orch.execute(manifest, skip_burn=True)

        # xorriso.create_iso called, burn_iso NOT called
        xorriso.create_iso.assert_called_once()
        xorriso.burn_iso.assert_not_called()
        # TEST_TINY ⇒ ECC implicitly skipped
        dvdisaster.augment_iso.assert_not_called()

        # Volume should be VERIFIED (via status update) and closed
        assert vol.status == "VERIFIED"
        assert vol.closed_at is not None

    def test_execute_invokes_ecc_for_production_media(self, orch_env):
        """ECC IS invoked when media_type.ecc_overhead_pct > 0 (production)."""
        orch = orch_env["orch"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)
        # Swap to a production media type with ECC overhead.
        manifest.media_type = MediaType.BD25

        # Override the staging-size preflight: real packs are far below the
        # BD25 capacity so this remains a pure orchestration test.
        orch.execute(manifest, skip_burn=True)

        dvdisaster.augment_iso.assert_called_once()

    def test_execute_with_burn(self, orch_env):
        """Physical burn is called when skip_burn=False."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)

        orch.execute(manifest, skip_burn=False)

        xorriso.burn_iso.assert_called_once()

    def test_execute_verify_rejects_wrong_volume_id(self, orch_env):
        """FMA-03: execute() must fail when the disc in the drive does
        not identify as the volume just burned."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)

        xorriso.read_disc_volume_id = MagicMock(
            return_value="SOME_OTHER_DISC",
        )

        with pytest.raises(ValueError, match="identifies as 'SOME_OTHER_DISC'"):
            orch.execute(manifest, skip_burn=False)

        # Failure reverts BURNING → STAGING like every other burn error.
        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_verify_rejects_device_hash_mismatch(self, orch_env):
        """FMA-03: execute() must fail when the device bytes do not hash
        to the ISO just burned (readable-but-wrong content)."""
        config = orch_env["config"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]
        orch = BurnOrchestrator(
            config, conn, xorriso, orch_env["dvdisaster"],
            device_reader=lambda device, length: "0" * 64,
        )
        manifest = self._prepare({**orch_env, "orch": orch})

        with pytest.raises(ValueError, match="device hash mismatch"):
            orch.execute(manifest, skip_burn=False)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_custom_iso_output(self, orch_env, tmp_path):
        """ISO output path can be overridden."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        manifest = self._prepare(orch_env)
        custom_iso = tmp_path / "custom" / "output.iso"

        orch.execute(manifest, iso_output=custom_iso, skip_burn=True)

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
            orch.execute(manifest, skip_burn=True)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_ecc_failure_reverts_status(self, orch_env):
        """If dvdisaster fails on production media, status reverts."""
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        dvdisaster = orch_env["dvdisaster"]
        manifest = self._prepare(orch_env)
        # ECC only runs on production media; swap to BD25 to exercise it.
        manifest.media_type = MediaType.BD25

        dvdisaster.augment_iso.side_effect = subprocess.CalledProcessError(1, "dvdisaster")

        with pytest.raises(subprocess.CalledProcessError):
            orch.execute(manifest, skip_burn=True)

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
            orch.execute(manifest, skip_burn=False)

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

        vol = orch.execute(manifest, skip_burn=True)
        assert vol.status == "VERIFIED"

    def test_execute_iso_unlink_failure_doesnt_rollback_burn(self, orch_env):
        """ISO unlink failure should not revert VERIFIED status (disc is safe)."""
        from unittest.mock import patch
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        manifest = self._prepare(orch_env)

        # Create a mock ISO file
        manifest.iso_path = Path(manifest.staging_path) / "test.iso"
        manifest.iso_path.write_bytes(b"ISO DATA")

        # Patch iso_path.unlink() to raise OSError
        with patch.object(Path, "unlink", side_effect=OSError("Permission denied")):
            vol = orch.execute(manifest, skip_burn=True)

        # Volume should still be VERIFIED (burn succeeded, ISO cleanup failed)
        assert vol.status == "VERIFIED"

        # Verify DB also shows VERIFIED
        db_vol = get_volume_by_id(conn, manifest.volume_id)
        assert db_vol.status == "VERIFIED"


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


# =========================================================================
# Preflight binary checks
# =========================================================================


class TestPreflightChecks:
    """execute() raises BinaryError before touching state if a tool is missing."""

    def _prepare(self, orch_env):
        return orch_env["orch"].prepare()

    def test_execute_fails_if_xorriso_missing(self, orch_env):
        """execute() raises BinaryError when xorriso not on PATH (before DB update)."""
        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import SubprocessRunnerBase
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        manifest = self._prepare(orch_env)

        # Replace mock with real runner whose binary check will fail
        real_xorriso = SubprocessRunnerBase.__new__(SubprocessRunnerBase)
        real_xorriso._binary = "xorriso_not_on_path_____"
        orch._xorriso = real_xorriso

        with pytest.raises(BinaryError, match="xorriso_not_on_path"):
            orch.execute(manifest, skip_burn=True)

        # Volume status must still be STAGING (no state change)
        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"

    def test_execute_fails_if_dvdisaster_missing(self, orch_env):
        """execute() raises BinaryError when dvdisaster missing on production media.

        ECC preflight only runs when ``media_type.ecc_overhead_pct > 0`` —
        we swap the manifest to BD25 to exercise the preflight path.
        """
        from lcsas.exceptions import BinaryError
        from lcsas.utils.subprocess import SubprocessRunnerBase
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        manifest = self._prepare(orch_env)
        manifest.media_type = MediaType.BD25

        real_dvd = SubprocessRunnerBase.__new__(SubprocessRunnerBase)
        real_dvd._binary = "dvdisaster_not_on_path_____"
        orch._dvdisaster = real_dvd

        with pytest.raises(BinaryError, match="dvdisaster_not_on_path"):
            orch.execute(manifest, skip_burn=True)

        vol = get_volume_by_id(conn, manifest.volume_id)
        assert vol.status == "STAGING"


# =========================================================================
# BurnOrchestrator.stage() — multi-volume session staging
# =========================================================================


class TestStage:
    """Unit tests for the session-based stage() method."""

    def test_stage_dry_run_no_side_effects(self, orch_env):
        """dry_run=True returns a plan with no DB writes or file system changes."""
        orch = orch_env["orch"]
        xorriso = orch_env["xorriso"]
        dvdisaster = orch_env["dvdisaster"]

        result = orch.stage(dry_run=True)

        assert result.session_id == "dry-run"
        assert result.manifests == []
        assert result.iso_paths == []
        # No ISO creation or ECC calls in dry-run
        xorriso.create_iso.assert_not_called()
        dvdisaster.augment_iso.assert_not_called()

    def test_stage_dry_run_returns_stage_result(self, orch_env):
        """dry_run=True returns a StageResult even without executing."""
        from lcsas.burn.orchestrator import StageResult
        orch = orch_env["orch"]
        result = orch.stage(dry_run=True)
        assert isinstance(result, StageResult)
        assert result.session_id == "dry-run"

    def test_stage_no_packs_raises(self, tmp_path):
        """stage() raises ValueError when there are no unarchived packs."""
        config = _make_config(tmp_path)
        conn = get_memory_connection()
        create_all(conn)

        # No packs registered at all — stage() should find nothing to stage.
        orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
        with pytest.raises(ValueError, match="No packs need staging"):
            orch.stage()

    def test_stage_creates_session_and_volumes(self, orch_env):
        """stage() with real packs creates a session and staged volumes."""
        from lcsas.db.sessions import get_session_volumes

        orch = orch_env["orch"]

        def fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
            output_iso.write_bytes(b"fake-iso-data")

        orch_env["xorriso"].create_iso.side_effect = fake_create_iso

        result = orch.stage()

        assert result.session_id != "dry-run"
        assert len(result.manifests) >= 1
        # Session recorded in DB
        session_vols = get_session_volumes(orch_env["conn"], result.session_id)
        assert len(session_vols) >= 1

    def test_stage_with_repo_filter(self, orch_env):
        """stage() with repo_ids filter only stages packs from that repo."""
        orch = orch_env["orch"]

        def fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
            output_iso.write_bytes(b"fake-iso-data")

        orch_env["xorriso"].create_iso.side_effect = fake_create_iso
        result = orch.stage(repo_ids=["family"])
        assert len(result.manifests) >= 1

    def test_stage_insufficient_disk_space_raises(self, orch_env):
        """stage() raises OSError when staging directory has insufficient space."""
        import unittest.mock

        orch = orch_env["orch"]
        with unittest.mock.patch("shutil.disk_usage") as mock_usage:
            mock_usage.return_value = unittest.mock.MagicMock(free=0)
            with pytest.raises(OSError, match="Insufficient disk space"):
                orch.stage()


# =========================================================================
# BURN-01: stage() must fail loud when a repo mirror is unavailable
# =========================================================================


def _make_two_repo_env(tmp_path):
    """Two registered repos, packs in both, fully-built mirrors on disk."""
    mirror = tmp_path / "mirror"
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)

    conn = get_memory_connection()
    create_all(conn)

    repos = {}
    for name in ("family", "work"):
        repo_dir = mirror / name
        (repo_dir / "data").mkdir(parents=True)
        for subdir in ("index", "snapshots", "keys"):
            (repo_dir / subdir).mkdir()
        (repo_dir / "config").write_text('{"version": 2}')
        repos[name] = RepositoryConfig(name=name, mirror_path=repo_dir)
        register_repo(conn, name, name.title(), str(repo_dir))

    config = LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=tmp_path / "archive.db",
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=1000,
        label_prefix="TEST",
        repositories=repos,
    )
    config.db_path.write_bytes(b"sqlite_placeholder")

    packs = []
    for i, repo in enumerate(["family", "work", "family", "work"], start=1):
        content = _pack_bytes(i, 100)
        sha = _sha(content)
        packs.append(register_pack(conn, sha256=sha, size_bytes=100, repo_id=repo))
        (mirror / repo / "data" / sha).write_bytes(content)

    orch = BurnOrchestrator(config, conn, MagicMock(), MagicMock())
    return orch, config, conn, packs


class TestStageFailsLoudOnMissingMirror:
    """BURN-01: a missing mirror must abort staging before any catalog write."""

    def test_stage_raises_when_repo_data_dir_missing(self, tmp_path):
        """One repo's data/ vanishes (e.g. NFS automount drop) → fail loud."""
        orch, config, conn, packs = _make_two_repo_env(tmp_path)

        shutil.rmtree(config.repositories["work"].mirror_path / "data")

        with pytest.raises(MirrorUnavailableError, match="work"):
            orch.stage()

        # Nothing committed: no volume row, every pack still unarchived.
        assert list_volumes(conn) == []
        assert len(get_unarchived_packs(conn)) == len(packs)

    def test_stage_raises_when_mirror_path_unregistered(self, tmp_path):
        """A DB repo row pointing at a nonexistent mirror path → fail loud."""
        orch, config, conn, packs = _make_two_repo_env(tmp_path)

        register_repo(conn, "ghost", "Ghost", str(tmp_path / "no-such-mirror"))
        register_pack(conn, sha256="f" * 64, size_bytes=100, repo_id="ghost")

        with pytest.raises(MirrorUnavailableError, match="ghost"):
            orch.stage()

        assert list_volumes(conn) == []
        assert len(get_unarchived_packs(conn)) == len(packs) + 1

    def test_stage_asserts_staged_count_equals_selected(self, tmp_path, monkeypatch):
        """Post-stage invariant catches a silent partial stage before commit."""
        orch, config, conn, packs = _make_two_repo_env(tmp_path)

        real_stage_packs = StagingBuilder.stage_packs

        def partial_stage(self, repo_packs, mirror_data_dir):
            # Silently drop the last pack — simulates a miscounting
            # stage_packs regression that does not raise on its own.
            return real_stage_packs(self, repo_packs[:-1], mirror_data_dir)

        monkeypatch.setattr(StagingBuilder, "stage_packs", partial_stage)

        with pytest.raises(MissingPacksError):
            orch.stage()

        assert list_volumes(conn) == []
        assert len(get_unarchived_packs(conn)) == len(packs)


# =========================================================================
# BURN-02: a corrupt mirror pack must abort staging before any catalog write
# =========================================================================


class TestStageCorruptPackCommitsNothing:
    """BURN-02: bit-rot on the mirror fails loud; the catalog stays untouched."""

    def test_stage_corrupt_pack_commits_nothing(self, tmp_path):
        orch, config, conn, packs = _make_two_repo_env(tmp_path)

        # Flip one byte of one seeded pack's mirror file (size unchanged —
        # only the content hash can catch this).
        victim = packs[0]
        victim_path = (
            config.repositories[victim.repo_id].mirror_path
            / "data" / victim.sha256
        )
        rotted = bytearray(victim_path.read_bytes())
        rotted[0] ^= 0xFF
        victim_path.write_bytes(bytes(rotted))

        with pytest.raises(CorruptPacksError, match="CORRUPT"):
            orch.stage()

        # Nothing committed: no volume row, every pack still unarchived.
        assert list_volumes(conn) == []
        assert len(get_unarchived_packs(conn)) == len(packs)


# =========================================================================
# BurnOrchestrator.burn_session()
# =========================================================================


class TestBurnSession:
    """Unit tests for the session-based burn_session() method."""

    def _create_staged_session(self, orch_env):
        """Stage packs and return the session ID."""
        orch = orch_env["orch"]

        def fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
            output_iso.write_bytes(b"fake-iso-data")

        orch_env["xorriso"].create_iso.side_effect = fake_create_iso
        result = orch.stage()
        return result.session_id

    def test_burn_session_skip_burn_succeeds(self, orch_env):
        """burn_session(skip_burn=True) records volume copies without burning."""
        from lcsas.db.volume_copies import get_copies_for_volume

        session_id = self._create_staged_session(orch_env)
        orch = orch_env["orch"]
        conn = orch_env["conn"]

        receipts = orch.burn_session(
            session_ref=session_id,
            location="Home_Shelf",
            skip_burn=True,
        )

        assert len(receipts) >= 1
        for receipt in receipts:
            copies = get_copies_for_volume(conn, receipt.volume_id)
            assert any(c.location == "Home_Shelf" for c in copies)

    def test_burn_session_latest_resolves(self, orch_env):
        """burn_session('latest') finds the most recent session."""
        self._create_staged_session(orch_env)
        orch = orch_env["orch"]
        receipts = orch.burn_session(session_ref="latest", skip_burn=True)
        assert isinstance(receipts, list)

    def test_burn_session_reburn_skips_status_transitions(self, orch_env):
        """Re-burn case: when volume is VERIFIED, skip status→BURNING transition."""
        from lcsas.db.volumes import update_status

        session_id = self._create_staged_session(orch_env)
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]

        # Get the volume from the session
        from lcsas.db.sessions import get_session_volumes
        vols = get_session_volumes(conn, session_id)
        volume_id = vols[0].volume_id

        # Mark it as VERIFIED (simulate a prior burn)
        update_status(conn, volume_id, "VERIFIED", force=True)

        # Mock xorriso methods
        xorriso.verify_disc.return_value = True
        xorriso.burn_iso.return_value = None

        # Burn again (re-burn to different location)
        receipts = orch.burn_session(
            session_ref=session_id,
            location="Remote_Archive",
            skip_burn=False,
        )

        # Should succeed and still be VERIFIED (not transitioned to BURNING)
        assert len(receipts) >= 1
        vol_after = get_volume_by_id(conn, volume_id)
        assert vol_after.status == "VERIFIED"

    def test_burn_session_verify_fail_on_reburn_records_event(self, orch_env):
        """Re-burn with verify failure records VERIFY_FAIL_REBURN event."""
        from lcsas.db.volume_events import get_events_for_volume
        from lcsas.db.volumes import update_status

        session_id = self._create_staged_session(orch_env)
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]

        # Get volume and mark as VERIFIED (simulating a prior successful burn)
        from lcsas.db.sessions import get_session_volumes
        vols = get_session_volumes(conn, session_id)
        volume_id = vols[0].volume_id
        update_status(conn, volume_id, "VERIFIED", force=True)

        # Mock: burn succeeds, but verify fails
        xorriso.verify_disc.return_value = False
        xorriso.burn_iso.return_value = None

        # Burn again to a different location (re-burn)
        receipts = orch.burn_session(
            session_ref=session_id,
            location="Remote_Archive",
            skip_burn=False,
        )

        # VERIFY_FAIL_REBURN event should be recorded
        events = get_events_for_volume(conn, volume_id)
        event_types = {e.event_type for e in events}
        assert "VERIFY_FAIL_REBURN" in event_types

        # Verify receipt indicates failure
        assert any(not r.verify_passed for r in receipts)

    def test_reburn_verify_fail_preserves_prior_copy(self, orch_env):
        """FMA-04: a verify-failed re-burn at the SAME location must leave
        the prior good copy row byte-identical (hash, burn_date) — a
        failed attempt to refresh a copy must not destroy the evidence
        of the copy that already exists."""
        from lcsas.db.sessions import get_session_volumes
        from lcsas.db.volume_copies import get_copies_for_volume
        from lcsas.db.volume_events import get_events_for_volume

        session_id = self._create_staged_session(orch_env)
        orch = orch_env["orch"]
        conn = orch_env["conn"]
        xorriso = orch_env["xorriso"]

        # First burn: verified copy at Home_Shelf. skip_burn keeps the
        # ISO on disk so the re-burn below has an image to burn from.
        orch.burn_session(session_ref=session_id, location="Home_Shelf",
                          skip_burn=True)

        vols = get_session_volumes(conn, session_id)
        volume_id = vols[0].volume_id
        before = get_copies_for_volume(conn, volume_id)
        assert len(before) == 1
        assert before[0].iso_sha256  # stage recorded a real hash

        # Re-burn to the SAME location, verify fails.
        xorriso.verify_disc.return_value = False
        xorriso.burn_iso.return_value = None
        receipts = orch.burn_session(session_ref=session_id,
                                     location="Home_Shelf", skip_burn=False)
        assert any(not r.verify_passed for r in receipts)

        after = get_copies_for_volume(conn, volume_id)
        assert len(after) == 1
        assert after[0].status == "ACTIVE"
        assert after[0].iso_sha256 == before[0].iso_sha256
        assert after[0].burn_date == before[0].burn_date

        events = get_events_for_volume(conn, volume_id)
        assert "VERIFY_FAIL_REBURN" in {e.event_type for e in events}


class TestReburnVerifyFailOnV5Catalog:
    """FMA-02: a catalog still on schema v5 (v4-era volume_events CHECK,
    no VERIFY_FAIL_REBURN) crashed with IntegrityError exactly when a bad
    re-burn needed recording.  Opening it through ensure_schema migrates
    it so the event lands cleanly."""

    def test_reburn_verify_fail_on_v5_catalog(self, tmp_path):
        from lcsas.db.schema import (
            CURRENT_SCHEMA_VERSION,
            ensure_schema,
            get_schema_version,
        )
        from lcsas.db.sessions import get_session_volumes
        from lcsas.db.volume_events import get_events_for_volume
        from tests.unit.test_db_schema import seed_v5_catalog

        config = _make_config(tmp_path)
        conn = get_memory_connection()
        seed_v5_catalog(conn)
        assert get_schema_version(conn) == 5

        # Prove the fixture bites: the v5 CHECK rejects the event type
        # (this is today's production crash without auto-migration).
        conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes) "
            "VALUES ('PROBE', 'probe-uuid', 'BD25', 1)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO volume_events (volume_id, event_type) "
                "SELECT volume_id, 'VERIFY_FAIL_REBURN' FROM volumes "
                "WHERE label = 'PROBE'"
            )
        conn.rollback()

        ensure_schema(conn)
        assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION

        _seed_db_and_mirror(conn, config)
        xorriso = MagicMock()

        def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
            Path(output_iso).parent.mkdir(parents=True, exist_ok=True)
            Path(output_iso).write_bytes(b"fake-iso-data")
            return Path(output_iso)

        xorriso.create_iso.side_effect = _fake_create_iso
        _wire_drive_identity(xorriso)

        def fake_device_reader(device: str, length_bytes: int) -> str:
            return hashlib.sha256(b"fake-iso-data"[:length_bytes]).hexdigest()

        orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                                device_reader=fake_device_reader)

        session_id = orch.stage().session_id
        vols = get_session_volumes(conn, session_id)
        volume_id = vols[0].volume_id
        update_status(conn, volume_id, "VERIFIED", force=True)

        # Re-burn whose post-burn verify fails.
        xorriso.verify_disc.return_value = False
        xorriso.burn_iso.return_value = None
        receipts = orch.burn_session(
            session_ref=session_id,
            location="Remote_Archive",
            skip_burn=False,
        )

        # The unhappy path records cleanly on the migrated catalog.
        events = get_events_for_volume(conn, volume_id)
        assert "VERIFY_FAIL_REBURN" in {e.event_type for e in events}
        assert any(not r.verify_passed for r in receipts)

