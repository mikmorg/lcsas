"""Comprehensive tests for the session-based stage/burn pipeline.

Includes:
- Multi-volume staging
- ISO creation with real xorriso (when available)
- ISO content validation (mount and inspect)
- Session-based burning (mocked disc writes)
- Multi-copy / multi-location burns
- Location sync (--for-location staging)
- Session manifests and burn receipts
- Clean-up operations
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.binpack.algorithm import first_fit_decreasing
from lcsas.burn.orchestrator import BurnOrchestrator, BurnReceipt, StageResult
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import create_location, list_locations
from lcsas.db.packs import register_pack
from lcsas.db.queries import (
    get_archive_status_summary,
    get_location_summary,
    get_packs_at_location,
    get_packs_missing_at_location,
    get_unarchived_or_missing_at_location,
    get_unarchived_packs,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_session, get_session_volumes, list_sessions
from lcsas.db.volume_copies import add_volume_copy, get_copies_for_volume
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import get_volume_by_id, list_volumes
from lcsas.iso.xorriso import SubprocessXorrisoRunner
from lcsas.utils.hashing import sha256_file

requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"), reason="xorriso not installed"
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, num_repos: int = 2,
                 media: MediaType = MediaType.TEST_TINY) -> LCSASConfig:
    """Build a test config with mirror repos and pack files on disk."""
    mirror = tmp_path / "mirror"
    staging = tmp_path / "staging"
    db_path = tmp_path / "archive.db"
    mirror.mkdir(parents=True, exist_ok=True)
    staging.mkdir(parents=True, exist_ok=True)

    repos = {}
    repo_names = ["family", "personal", "work"][:num_repos]
    for name in repo_names:
        repo_dir = mirror / name
        data_dir = repo_dir / "data"
        data_dir.mkdir(parents=True, exist_ok=True)
        for subdir in ["index", "snapshots", "keys"]:
            (repo_dir / subdir).mkdir(exist_ok=True)
            (repo_dir / subdir / "dummy.json").write_text('{"test": true}')
        (repo_dir / "config").write_text('{"version": 2}')
        repos[name] = RepositoryConfig(name=name, mirror_path=repo_dir)

    return LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=db_path,
        default_media_type=media,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=100,
        label_prefix="TEST",
        repositories=repos,
    )


def _seed_packs(conn, config: LCSASConfig, num_packs: int = 10,
                pack_size: int = 50) -> list:
    """Register packs in DB and create matching files in mirror data dirs."""
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    # Write a real SQLite DB for holographic injection
    from lcsas.db.connection import get_connection
    db_conn = get_connection(config.db_path)
    create_all(db_conn)
    db_conn.close()

    repo_names = list(config.repositories.keys())
    packs = []
    for i in range(1, num_packs + 1):
        repo_name = repo_names[(i - 1) % len(repo_names)]
        sha = f"{i:064x}"
        p = register_pack(conn, sha256=sha, size_bytes=pack_size,
                           repo_id=repo_name)
        packs.append(p)

        # Create pack file in mirror
        data_dir = config.repositories[repo_name].mirror_path / "data"
        pack_file = data_dir / sha
        pack_file.write_bytes(os.urandom(pack_size))

    return packs


@pytest.fixture
def env(tmp_path):
    """Full test environment with config, DB, repos, packs, and orchestrator."""
    config = _make_config(tmp_path, num_repos=2)
    conn = get_memory_connection()
    create_all(conn)

    # Register repos in DB
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))

    # Seed packs (small enough to fit in TEST_TINY with metadata reserve)
    packs = _seed_packs(conn, config, num_packs=5, pack_size=50)

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
        "tmp_path": tmp_path,
    }


@pytest.fixture
def multi_vol_env(tmp_path):
    """Environment where packs require multiple volumes (TEST_TINY = 1MB)."""
    config = _make_config(tmp_path, num_repos=2, media=MediaType.TEST_TINY)
    conn = get_memory_connection()
    create_all(conn)

    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))

    # Each pack is ~400KB, so 3 packs won't fit in 1MB with 100 bytes reserve
    # TEST_TINY capacity = 1_048_576 bytes, usable = 1_048_576 (0% ecc)
    # 3 packs @ 400KB = 1.2MB > 1MB → needs 2 volumes
    packs = _seed_packs(conn, config, num_packs=3, pack_size=400_000)

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
        "tmp_path": tmp_path,
    }


# =========================================================================
# Stage Tests
# =========================================================================


class TestStage:
    def test_stage_single_volume(self, env):
        """Stage all packs into a single volume."""
        result = env["orch"].stage()

        assert isinstance(result, StageResult)
        assert len(result.manifests) == 1
        assert len(result.iso_paths) == 1
        assert result.session_id

        # Session manifest should exist
        manifest_path = result.staging_dir / "session.json"
        assert manifest_path.is_file()

        with open(manifest_path) as f:
            manifest = json.load(f)
        assert manifest["session_id"] == result.session_id
        assert len(manifest["volumes"]) == 1

    def test_stage_multi_volume(self, multi_vol_env):
        """Data exceeding one disc creates multiple volumes."""
        result = multi_vol_env["orch"].stage()

        assert len(result.manifests) >= 2
        assert len(result.iso_paths) >= 2

        # Each manifest covers different packs
        all_pack_ids = set()
        for m in result.manifests:
            pack_ids = {p.pack_id for p in m.selected_packs}
            # No overlap between volumes
            assert all_pack_ids.isdisjoint(pack_ids)
            all_pack_ids.update(pack_ids)

        # All packs covered
        assert len(all_pack_ids) == len(multi_vol_env["packs"])

    def test_stage_creates_session_in_db(self, env):
        """Session is recorded in the database."""
        result = env["orch"].stage()

        session = get_session(env["conn"], result.session_id)
        assert session.status == "STAGED"
        assert session.media_type == "TEST_TINY"

        session_vols = get_session_volumes(env["conn"], result.session_id)
        assert len(session_vols) == len(result.manifests)

    def test_stage_registers_volumes_in_db(self, env):
        """Each staged volume is registered with STAGING status."""
        result = env["orch"].stage()

        for m in result.manifests:
            vol = get_volume_by_id(env["conn"], m.volume_id)
            assert vol.status == "STAGING"
            assert vol.media_type == "TEST_TINY"

            linked = get_pack_ids_for_volume(env["conn"], m.volume_id)
            assert len(linked) == len(m.selected_packs)

    def test_stage_nothing_to_stage_raises(self, env):
        """Raises when no unarchived packs exist."""
        env["orch"].stage()
        with pytest.raises(ValueError, match="No packs need staging"):
            env["orch"].stage()

    def test_stage_with_ecc(self, env):
        """ECC augmentation is called when not skipped."""
        env["orch"].stage(skip_ecc=False)
        env["dvdisaster"].augment_iso.assert_called()

    def test_stage_skip_ecc(self, env):
        """ECC is not called when skip_ecc=True."""
        env["orch"].stage(skip_ecc=True)
        env["dvdisaster"].augment_iso.assert_not_called()

    def test_stage_xorriso_called(self, env):
        """xorriso.create_iso is called for each volume."""
        result = env["orch"].stage()
        assert env["xorriso"].create_iso.call_count == len(result.manifests)

    def test_stage_staging_dir_structure(self, env):
        """Staging directory has expected structure per volume."""
        result = env["orch"].stage()

        for m in result.manifests:
            assert m.staging_path.is_dir()
            assert (m.staging_path / "data").is_dir()
            assert (m.staging_path / "volume_info.json").is_file()
            assert (m.staging_path / "catalog.db").is_file()

            # Data dir contains pack files
            data_files = list((m.staging_path / "data").iterdir())
            assert len(data_files) > 0

    def test_stage_for_location_unarchived(self, env):
        """--for-location stages all packs when location has no copies."""
        conn = env["conn"]
        create_location(conn, "Empty_Location")

        result = env["orch"].stage(for_location="Empty_Location")
        total_packs = sum(len(m.selected_packs) for m in result.manifests)
        assert total_packs == len(env["packs"])


class TestStageForLocation:
    """Tests for location-targeted staging (--for-location)."""

    def test_stage_for_location_delta(self, tmp_path):
        """Stage only packs missing at a specific location."""
        config = _make_config(tmp_path)
        conn = get_memory_connection()
        create_all(conn)

        for name in config.repositories:
            register_repo(conn, name, name.title(),
                          str(config.repositories[name].mirror_path))
        packs = _seed_packs(conn, config, num_packs=5, pack_size=50)

        create_location(conn, "Home_Shelf")
        create_location(conn, "Offsite_Safe")

        # Manually stage and burn first 3 packs to both locations
        xorriso = MagicMock()
        dvdisaster = MagicMock()
        orch = BurnOrchestrator(config, conn, xorriso, dvdisaster)

        # First: stage all packs
        result = orch.stage(skip_ecc=True)

        # Burn to Home_Shelf
        orch.burn_session(result.session_id, "Home_Shelf", skip_burn=True)

        # Burn only first volume's worth to Offsite_Safe
        # (Can't do partial, so let's simulate differently)
        # Instead, we add copies manually for packs[0:3] only
        # First get the volumes
        session_vols = get_session_volumes(conn, result.session_id)
        # Add copy at Offsite_Safe for all volumes
        for sv in session_vols:
            add_volume_copy(conn, sv.volume_id, "Offsite_Safe")

        # Now register 2 more packs (simulating next month)
        for i in range(6, 8):
            sha = f"{i:064x}"
            repo_name = list(config.repositories.keys())[0]
            register_pack(conn, sha256=sha, size_bytes=50, repo_id=repo_name)
            data_dir = config.repositories[repo_name].mirror_path / "data"
            (data_dir / sha).write_bytes(os.urandom(50))

        # Stage for Offsite_Safe should pick up only the 2 new unarchived packs
        result2 = orch.stage(for_location="Offsite_Safe", skip_ecc=True)
        total_packs = sum(len(m.selected_packs) for m in result2.manifests)
        assert total_packs == 2


# =========================================================================
# Burn Session Tests
# =========================================================================


class TestBurnSession:
    def test_burn_session_happy_path(self, env):
        """Burn a staged session — volumes get VERIFIED, copies created."""
        conn = env["conn"]
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        receipts = orch.burn_session(result.session_id, "Home_Shelf",
                                     skip_burn=True)

        assert len(receipts) == len(result.manifests)

        for receipt in receipts:
            assert receipt.location == "Home_Shelf"
            assert receipt.pack_count > 0

            # Volume should be VERIFIED
            vol = get_volume_by_id(conn, receipt.volume_id)
            assert vol.status == "VERIFIED"

            # Copy should exist
            copies = get_copies_for_volume(conn, receipt.volume_id)
            assert len(copies) == 1
            assert copies[0].location == "Home_Shelf"

    def test_burn_session_latest(self, env):
        """'latest' resolves to the most recent session."""
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        receipts = orch.burn_session("latest", "Home_Shelf", skip_burn=True)

        assert receipts[0].session_id == result.session_id

    def test_burn_session_multi_location(self, env):
        """Same session can be burned to multiple locations."""
        conn = env["conn"]
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)

        # Burn copy 1
        receipts1 = orch.burn_session(result.session_id, "Home_Shelf",
                                      skip_burn=True)
        # Burn copy 2
        receipts2 = orch.burn_session(result.session_id, "Offsite_Safe",
                                      skip_burn=True)

        assert len(receipts1) == len(receipts2)

        for receipt in receipts1:
            copies = get_copies_for_volume(conn, receipt.volume_id)
            locations = {c.location for c in copies}
            assert "Home_Shelf" in locations
            assert "Offsite_Safe" in locations

    def test_burn_session_creates_receipts(self, env):
        """Burn receipts are written as JSON files."""
        orch = env["orch"]
        result = orch.stage(skip_ecc=True)
        receipts = orch.burn_session(result.session_id, "Home_Shelf",
                                     skip_burn=True)

        # Check receipt files exist
        receipts_dir = result.staging_dir / "receipts"
        assert receipts_dir.is_dir()

        receipt_files = list(receipts_dir.glob("*.json"))
        assert len(receipt_files) == len(receipts)

        # Validate receipt content
        for rf in receipt_files:
            with open(rf) as f:
                data = json.load(f)
            assert "volume_label" in data
            assert "location" in data
            assert "burn_date" in data

    def test_burn_session_updates_session_status(self, env):
        """Session status transitions to COMPLETE after burn."""
        conn = env["conn"]
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        orch.burn_session(result.session_id, "Home_Shelf", skip_burn=True)

        session = get_session(conn, result.session_id)
        assert session.status == "COMPLETE"

    def test_burn_session_location_auto_created(self, env):
        """Burning to a new location auto-creates the location."""
        conn = env["conn"]
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        orch.burn_session(result.session_id, "New_Location", skip_burn=True)

        locs = list_locations(conn)
        loc_names = {l.name for l in locs}
        assert "New_Location" in loc_names

    def test_burn_session_verify_pass_records_event(self, env):
        """When burn+verify passes, a VERIFY_PASS event is recorded."""
        from lcsas.db.volume_events import get_events_for_volume
        conn = env["conn"]
        orch = env["orch"]
        xorriso = env["xorriso"]

        result = orch.stage(skip_ecc=True)

        # Mock physical burn and verification to succeed
        xorriso.burn_iso = MagicMock()
        xorriso.verify_disc = MagicMock(return_value=True)

        receipts = orch.burn_session(result.session_id, "Home_Shelf",
                                     skip_burn=False)

        for receipt in receipts:
            assert receipt.verify_passed is True
            vol = get_volume_by_id(conn, receipt.volume_id)
            assert vol.status == "VERIFIED"

            events = get_events_for_volume(conn, receipt.volume_id, "VERIFY_PASS")
            assert len(events) >= 1
            assert "Post-burn read-back" in events[0].detail

    def test_burn_session_verify_fail_stays_burned(self, env):
        """When verify fails, volume stays BURNED and event is recorded."""
        from lcsas.db.volume_events import get_events_for_volume
        conn = env["conn"]
        orch = env["orch"]
        xorriso = env["xorriso"]

        result = orch.stage(skip_ecc=True)

        # Mock physical burn to succeed but verification to fail
        xorriso.burn_iso = MagicMock()
        xorriso.verify_disc = MagicMock(return_value=False)

        receipts = orch.burn_session(result.session_id, "Home_Shelf",
                                     skip_burn=False)

        for receipt in receipts:
            assert receipt.verify_passed is False
            vol = get_volume_by_id(conn, receipt.volume_id)
            assert vol.status == "BURNED"

            events = get_events_for_volume(conn, receipt.volume_id, "VERIFY_FAIL")
            assert len(events) >= 1
            assert "failed" in events[0].detail.lower()


# =========================================================================
# Clean Session Tests
# =========================================================================


class TestCleanSession:
    def test_clean_removes_staging_dir(self, env):
        """Clean session removes the staging directory."""
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        assert result.staging_dir.is_dir()

        orch.clean_session(result.session_id)
        assert not result.staging_dir.exists()

    def test_clean_updates_session_status(self, env):
        """Session status set to CLEANED after cleanup."""
        conn = env["conn"]
        orch = env["orch"]

        result = orch.stage(skip_ecc=True)
        orch.clean_session(result.session_id)

        session = get_session(conn, result.session_id)
        assert session.status == "CLEANED"

    def test_clean_latest(self, env):
        """'latest' works for clean."""
        orch = env["orch"]
        result = orch.stage(skip_ecc=True)
        orch.clean_session("latest")

        session = get_session(env["conn"], result.session_id)
        assert session.status == "CLEANED"


# =========================================================================
# Multi-Volume Pipeline Integration Tests
# =========================================================================


class TestMultiVolumePipeline:
    def test_full_pipeline_multi_volume(self, multi_vol_env):
        """Full pipeline: stage multi-volume → burn copy1 → burn copy2."""
        conn = multi_vol_env["conn"]
        orch = multi_vol_env["orch"]

        # Stage
        result = orch.stage(skip_ecc=True)
        assert len(result.manifests) >= 2

        # Burn copy 1
        receipts1 = orch.burn_session(result.session_id, "Home_Shelf",
                                      skip_burn=True)
        assert len(receipts1) >= 2

        # Burn copy 2
        receipts2 = orch.burn_session(result.session_id, "Offsite_Safe",
                                      skip_burn=True)

        # Every pack should have 2 copies
        for m in result.manifests:
            copies = get_copies_for_volume(conn, m.volume_id)
            assert len(copies) == 2

        # Summary
        summary = get_location_summary(conn)
        for s in summary:
            assert s["missing"] == 0

    def test_incremental_stage_after_burn(self, env):
        """After burning, new packs can be staged in a new session."""
        conn = env["conn"]
        orch = env["orch"]
        config = env["config"]

        # Session 1: stage & burn all
        r1 = orch.stage(skip_ecc=True)
        orch.burn_session(r1.session_id, "Home_Shelf", skip_burn=True)

        # Add new packs
        for i in range(20, 25):
            sha = f"{i:064x}"
            repo_name = list(config.repositories.keys())[0]
            register_pack(conn, sha256=sha, size_bytes=50, repo_id=repo_name)
            data_dir = config.repositories[repo_name].mirror_path / "data"
            (data_dir / sha).write_bytes(os.urandom(50))

        # Session 2: stage only new packs
        r2 = orch.stage(skip_ecc=True)
        total_packs = sum(len(m.selected_packs) for m in r2.manifests)
        assert total_packs == 5  # only the 5 new ones

        # Sessions should be distinct
        sessions = list_sessions(conn)
        assert len(sessions) == 2
        assert sessions[0].session_id != sessions[1].session_id


# =========================================================================
# ISO Content Validation Tests (requires xorriso)
# =========================================================================


@requires_xorriso
class TestISOContentValidation:
    """Tests that create actual ISO files and validate their contents.

    These use the real xorriso binary to create ISOs, then inspect them.
    No physical disc burning occurs.
    """

    def _make_real_orch(self, tmp_path, num_packs=3, pack_size=1024):
        """Create an orchestrator with real xorriso (mocked dvdisaster)."""
        config = _make_config(tmp_path, num_repos=2, media=MediaType.TEST_SMALL)
        conn = get_memory_connection()
        create_all(conn)

        for name in config.repositories:
            register_repo(conn, name, name.title(),
                          str(config.repositories[name].mirror_path))
        packs = _seed_packs(conn, config, num_packs=num_packs,
                            pack_size=pack_size)

        xorriso = SubprocessXorrisoRunner()
        dvdisaster = MagicMock()

        orch = BurnOrchestrator(config, conn, xorriso, dvdisaster)
        return orch, config, conn, packs

    def test_iso_created_and_valid(self, tmp_path):
        """Stage creates a valid ISO file."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for iso_path in result.iso_paths:
            assert iso_path.exists()
            assert iso_path.stat().st_size > 0

            # Verify ISO structure with xorriso
            cmd = ["xorriso", "-indev", str(iso_path), "-ls", "/"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0

    def test_iso_contains_data_dir(self, tmp_path):
        """ISO contains a data/ directory with pack files."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for iso_path in result.iso_paths:
            cmd = ["xorriso", "-indev", str(iso_path), "-ls", "/data/"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0
            # Output should list pack files
            output = proc.stdout + proc.stderr
            # Each line in directory listing is a file
            files_in_data = [
                line.strip().strip("'")
                for line in output.splitlines()
                if line.strip() and not line.startswith("-") and line.strip() != "/"
            ]
            assert len(files_in_data) > 0

    def test_iso_contains_metadata(self, tmp_path):
        """ISO contains metadata/ directory."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for iso_path in result.iso_paths:
            cmd = ["xorriso", "-indev", str(iso_path), "-ls", "/metadata/"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0

    def test_iso_contains_catalog(self, tmp_path):
        """ISO contains catalog.db file."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for iso_path in result.iso_paths:
            cmd = ["xorriso", "-indev", str(iso_path),
                   "-find", "/", "-name", "catalog.db"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0
            output = proc.stdout + proc.stderr
            assert "catalog.db" in output

    def test_iso_contains_volume_info(self, tmp_path):
        """ISO contains volume_info.json file."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for iso_path in result.iso_paths:
            cmd = ["xorriso", "-indev", str(iso_path),
                   "-find", "/", "-name", "volume_info.json"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0
            output = proc.stdout + proc.stderr
            assert "volume_info.json" in output

    def test_iso_extract_and_validate_packs(self, tmp_path):
        """Extract packs from ISO and verify content matches originals."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for i, manifest in enumerate(result.manifests):
            iso_path = result.iso_paths[i]
            extract_dir = tmp_path / f"extract_{i}"
            extract_dir.mkdir()

            # Extract ISO contents
            cmd = [
                "xorriso", "-osirrox", "on",
                "-indev", str(iso_path),
                "-extract", "/", str(extract_dir),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0

            # Verify pack files exist and match originals
            extracted_data = extract_dir / "data"
            assert extracted_data.is_dir()

            for pack in manifest.selected_packs:
                # Find original pack file
                original_path = None
                for repo_name, repo_cfg in config.repositories.items():
                    candidate = repo_cfg.mirror_path / "data" / pack.sha256
                    if candidate.exists():
                        original_path = candidate
                        break

                if original_path is None:
                    continue  # Pack was in a repo we didn't create file for

                # Find extracted pack file
                extracted_path = extracted_data / pack.sha256
                assert extracted_path.exists(), \
                    f"Pack {pack.sha256} missing from ISO extraction"

                # Compare file contents
                orig_hash = sha256_file(original_path)
                extr_hash = sha256_file(extracted_path)
                assert orig_hash == extr_hash, \
                    f"Pack {pack.sha256} content mismatch after ISO round-trip"

    def test_iso_extract_volume_info_valid_json(self, tmp_path):
        """Extract volume_info.json from ISO and validate it."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        for i, manifest in enumerate(result.manifests):
            iso_path = result.iso_paths[i]
            extract_dir = tmp_path / f"extract_vi_{i}"
            extract_dir.mkdir()

            cmd = [
                "xorriso", "-osirrox", "on",
                "-indev", str(iso_path),
                "-extract", "/volume_info.json",
                str(extract_dir / "volume_info.json"),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0

            with open(extract_dir / "volume_info.json") as f:
                vi = json.load(f)

            assert vi["label"] == manifest.volume_label
            assert vi["uuid"] == manifest.volume_uuid
            assert vi["media_type"] == "TEST_SMALL"

    def test_multi_volume_iso_all_packs_covered(self, tmp_path):
        """When staging produces multiple volumes, all packs are in ISOs."""
        # Use TEST_SMALL with packs sized to force multi-volume but
        # stay under media capacity including ISO filesystem overhead.
        config = _make_config(tmp_path, num_repos=1, media=MediaType.TEST_SMALL)
        conn = get_memory_connection()
        create_all(conn)

        for name in config.repositories:
            register_repo(conn, name, name.title(),
                          str(config.repositories[name].mirror_path))

        # Create packs that require 2+ volumes on TEST_SMALL (10 MB, 10% ECC = 9 MB usable)
        packs = _seed_packs(conn, config, num_packs=5, pack_size=2_000_000)

        xorriso = SubprocessXorrisoRunner()
        dvdisaster = MagicMock()
        orch = BurnOrchestrator(config, conn, xorriso, dvdisaster)

        result = orch.stage(skip_ecc=True)
        assert len(result.manifests) >= 2

        # Extract all ISOs and collect all pack files
        all_extracted_packs = set()
        for i, iso_path in enumerate(result.iso_paths):
            extract_dir = tmp_path / f"multi_extract_{i}"
            extract_dir.mkdir()

            cmd = [
                "xorriso", "-osirrox", "on",
                "-indev", str(iso_path),
                "-extract", "/", str(extract_dir),
            ]
            subprocess.run(cmd, capture_output=True, text=True, check=True)

            data_dir = extract_dir / "data"
            if data_dir.is_dir():
                for f in data_dir.iterdir():
                    all_extracted_packs.add(f.name)

        # Every pack should appear exactly once across all ISOs
        expected_shas = {p.sha256 for p in packs}
        assert all_extracted_packs == expected_shas

    def test_session_manifest_matches_isos(self, tmp_path):
        """session.json accurately describes the staged ISOs."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        result = orch.stage(skip_ecc=True)

        manifest_path = result.staging_dir / "session.json"
        with open(manifest_path) as f:
            manifest = json.load(f)

        assert len(manifest["volumes"]) == len(result.manifests)
        for vol_info in manifest["volumes"]:
            iso_path = Path(vol_info["iso_path"])
            assert iso_path.exists()
            assert vol_info["pack_count"] > 0

    def test_full_pipeline_with_real_iso(self, tmp_path):
        """End-to-end: stage with real ISO → burn (skip disc) → verify."""
        orch, config, conn, packs = self._make_real_orch(tmp_path)

        create_location(conn, "Home_Shelf")
        create_location(conn, "Offsite_Safe")

        # Stage
        result = orch.stage(skip_ecc=True)
        assert len(result.iso_paths) >= 1

        # Burn copy 1
        receipts1 = orch.burn_session(result.session_id, "Home_Shelf",
                                      skip_burn=True)

        # Burn copy 2
        receipts2 = orch.burn_session(result.session_id, "Offsite_Safe",
                                      skip_burn=True)

        # Verify all packs archived
        summary = get_archive_status_summary(conn)
        assert summary["unarchived"] == 0

        # Verify location summary
        loc_summary = get_location_summary(conn)
        assert len(loc_summary) == 2
        for s in loc_summary:
            assert s["missing"] == 0

        # Verify ISO files are valid
        for iso_path in result.iso_paths:
            cmd = ["xorriso", "-indev", str(iso_path), "-ls", "/"]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            assert proc.returncode == 0

        # Clean
        orch.clean_session(result.session_id)
        assert not result.staging_dir.exists()


# =========================================================================
# Session Manifest and Receipt Tests
# =========================================================================


class TestSessionManifestAndReceipts:
    def test_session_manifest_schema(self, env):
        """Session manifest has the required fields."""
        result = env["orch"].stage(skip_ecc=True)

        manifest_path = result.staging_dir / "session.json"
        with open(manifest_path) as f:
            manifest = json.load(f)

        assert "session_id" in manifest
        assert "created_at" in manifest
        assert "media_type" in manifest
        assert "status" in manifest
        assert "volumes" in manifest
        assert isinstance(manifest["volumes"], list)

        for vol in manifest["volumes"]:
            assert "volume_id" in vol
            assert "label" in vol
            assert "uuid" in vol
            assert "iso_path" in vol
            assert "pack_count" in vol
            assert "pack_ids" in vol

    def test_burn_receipt_schema(self, env):
        """Burn receipts have the required fields."""
        orch = env["orch"]
        result = orch.stage(skip_ecc=True)
        receipts = orch.burn_session(result.session_id, "Home_Shelf",
                                     skip_burn=True)

        receipts_dir = result.staging_dir / "receipts"
        for rf in receipts_dir.glob("*.json"):
            with open(rf) as f:
                data = json.load(f)

            assert "volume_label" in data
            assert "volume_id" in data
            assert "session_id" in data
            assert "location" in data
            assert "device" in data
            assert "burn_date" in data
            assert "iso_sha256" in data
            assert "verify_passed" in data
            assert "pack_count" in data
            assert "pack_ids" in data

    def test_multiple_burn_locations_separate_receipts(self, env):
        """Each location burn creates separate receipt files."""
        orch = env["orch"]
        result = orch.stage(skip_ecc=True)

        orch.burn_session(result.session_id, "Home_Shelf", skip_burn=True)
        orch.burn_session(result.session_id, "Offsite_Safe", skip_burn=True)

        receipts_dir = result.staging_dir / "receipts"
        receipt_files = list(receipts_dir.glob("*.json"))

        # Should have 2× receipts (one per volume per location)
        expected_count = len(result.manifests) * 2
        assert len(receipt_files) == expected_count

        # Check that both locations are represented
        locations_seen = set()
        for rf in receipt_files:
            with open(rf) as f:
                data = json.load(f)
            locations_seen.add(data["location"])
        assert locations_seen == {"Home_Shelf", "Offsite_Safe"}


# =========================================================================
# Backward Compatibility Tests (legacy prepare/execute still works)
# =========================================================================


class TestLegacyPrepareExecute:
    """Verify the existing prepare()/execute() API still works."""

    def test_prepare_still_works(self, env):
        """Legacy prepare() still creates a single-volume manifest."""
        orch = env["orch"]
        manifest = orch.prepare()

        assert manifest.volume_label.startswith("TEST_")
        assert manifest.total_data_bytes > 0
        assert len(manifest.selected_packs) > 0

    def test_execute_still_works(self, env):
        """Legacy execute() still creates ISO and finalizes volume."""
        orch = env["orch"]
        manifest = orch.prepare()
        vol = orch.execute(manifest, skip_burn=True, skip_ecc=True)

        assert vol.status == "VERIFIED"

    def test_abort_still_works(self, env):
        """Legacy abort() still cleans up."""
        orch = env["orch"]
        manifest = orch.prepare()
        orch.abort(manifest)
        assert not manifest.staging_path.exists()
