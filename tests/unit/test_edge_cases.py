"""Tests for edge cases and boundary conditions across modules."""

from __future__ import annotations

import sys

import pytest

from lcsas.binpack.algorithm import estimate_volumes_needed, first_fit_decreasing
from lcsas.config.media import MediaType
from lcsas.config.settings import load_config
from lcsas.db.packs import register_pack
from lcsas.db.queries import get_archive_status_summary, get_pick_list, get_redundancy_report
from lcsas.db.repos import register_repo
from lcsas.db.volumes import create_volume, update_status, update_used_bytes
from lcsas.packs.scanner import scan_mirror_packs
from lcsas.utils.labels import generate_uuid

# =========================================================================
# binpack edge cases
# =========================================================================


class TestBinpackEdgeCases:
    def test_zero_size_items(self):
        """Zero-byte items should fit (they take no space)."""
        items = [("a", 0), ("b", 0), ("c", 0)]
        selected, remaining = first_fit_decreasing(items, capacity=100)
        assert len(selected) == 3
        assert remaining == []

    def test_single_item_exact_fit(self):
        """One item exactly equal to usable capacity."""
        items = [("a", 100)]
        selected, remaining = first_fit_decreasing(items, capacity=100, reserved=0)
        assert len(selected) == 1

    def test_single_item_with_reserved_exact(self):
        """Item fits exactly after reserved space."""
        items = [("a", 80)]
        selected, remaining = first_fit_decreasing(items, capacity=100, reserved=20)
        assert len(selected) == 1

    def test_all_items_too_large(self):
        """All items exceed capacity → none selected."""
        items = [("a", 200), ("b", 300)]
        selected, remaining = first_fit_decreasing(items, capacity=100, reserved=0)
        assert selected == []
        assert len(remaining) == 2

    def test_empty_items_list(self):
        selected, remaining = first_fit_decreasing([], capacity=100)
        assert selected == []
        assert remaining == []

    def test_estimate_volumes_needed_zero_bytes(self):
        est = estimate_volumes_needed(0, MediaType.TEST_TINY.capacity_bytes)
        assert est == 0

    def test_estimate_volumes_needed_exact_fit(self):
        mt = MediaType.TEST_TINY
        est = estimate_volumes_needed(
            mt.usable_bytes, mt.capacity_bytes,
            ecc_overhead_pct=mt.ecc_overhead_pct,
        )
        assert est == 1


# =========================================================================
# config edge cases
# =========================================================================


class TestConfigEdgeCases:
    def test_load_nonexistent_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "nonexistent.toml")

    def test_load_malformed_toml(self, tmp_path):
        import tomllib

        bad = tmp_path / "bad.toml"
        bad.write_text("[[[[invalid toml")
        with pytest.raises(tomllib.TOMLDecodeError):
            load_config(bad)

    def test_load_invalid_media_type(self, tmp_path):
        cfg = tmp_path / "cfg.toml"
        cfg.write_text('[defaults]\nmedia_type = "NONEXISTENT"\n')
        with pytest.raises(ValueError, match="Unknown media type"):
            load_config(cfg)

    def test_media_type_enum_properties(self):
        """Verify all media type properties are consistent."""
        for mt in MediaType:
            assert mt.usable_bytes <= mt.capacity_bytes
            assert mt.ecc_overhead_pct >= 0
            assert mt.usable_bytes >= 0


# =========================================================================
# db edge cases
# =========================================================================


class TestDbEdgeCases:
    def test_update_status_invalid(self, memory_db):
        """Invalid status is caught by transition enforcement."""
        vol = create_volume(
            memory_db, label="TEST_V", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=25_000_000_000,
        )
        with pytest.raises(ValueError):
            update_status(memory_db, vol.volume_id, "INVALID_STATUS")

    def test_update_used_bytes_exceeds_capacity(self, memory_db):
        """DB doesn't prevent used_bytes > capacity_bytes (no constraint)."""
        vol = create_volume(
            memory_db, label="OVR", uuid=generate_uuid(),
            media_type="BD25", capacity_bytes=100,
        )
        # Should not raise — the DB doesn't enforce this
        update_used_bytes(memory_db, vol.volume_id, 999)

    def test_register_pack_zero_size(self, memory_db):
        """Zero-byte pack is accepted."""
        register_repo(memory_db, "r", "R", "/r")
        p = register_pack(memory_db, sha256="zero_pack", size_bytes=0, repo_id="r")
        assert p.size_bytes == 0

    def test_pick_list_dedup_prefers_first_volume(self, populated_db):
        """Packs on multiple volumes: pick_list groups by volume."""
        needed = ["pack_0001_hash"]  # on vol1 and vol4
        pick = get_pick_list(populated_db, needed)
        # Should appear on at least one volume
        all_packs = [p for packs in pick.values() for p in packs]
        assert len(all_packs) == 1
        assert all_packs[0].sha256 == "pack_0001_hash"

    def test_redundancy_report_min_copies_zero(self, populated_db):
        """With min_copies=0, no packs should appear (all have ≥0 copies)."""
        under = get_redundancy_report(populated_db, min_copies=0)
        assert len(under) == 0

    def test_archive_status_summary_empty(self, memory_db):
        """Summary on empty DB returns all zeros."""
        summary = get_archive_status_summary(memory_db)
        assert summary == {"total": 0, "pruned": 0, "archived": 0, "unarchived": 0}


# =========================================================================
# scanner edge cases
# =========================================================================


class TestScannerEdgeCases:
    def test_empty_mirror(self, tmp_path):
        """Empty data directory returns empty dict."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        result = scan_mirror_packs(tmp_path)
        assert result == {}

    def test_nonexistent_data_dir(self, tmp_path):
        """No data/ directory returns empty dict."""
        result = scan_mirror_packs(tmp_path)
        assert result == {}

    def test_zero_byte_pack(self, tmp_path):
        """Zero-byte pack file is recorded with size 0."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        sha = "0" * 64
        (data_dir / sha).write_bytes(b"")
        result = scan_mirror_packs(tmp_path)
        assert sha in result
        assert result[sha] == 0

    @pytest.mark.skipif(sys.platform == "win32", reason="Symlinks not standard on Windows")
    def test_symlinked_pack_included(self, tmp_path):
        """Symlinked pack files are followed and included."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        real = tmp_path / "real_pack"
        real.write_bytes(b"x" * 50)
        sha = "s" * 64
        (data_dir / sha).symlink_to(real)

        result = scan_mirror_packs(tmp_path)
        assert sha in result
        assert result[sha] == 50
