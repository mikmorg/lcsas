"""Unit tests for db/verify.py — disc validation."""

from pathlib import Path
import json
import sqlite3

import pytest

from lcsas.db.verify import (
    CatalogValidationResult,
    _collect_disc_packs,
    validate_disc,
)


class TestCollectDiscPacks:
    def test_flat_layout(self, tmp_path):
        """Collect packs in flat data/HASH layout."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        pack_sha1 = "a" * 64
        pack_sha2 = "b" * 64
        (data_dir / pack_sha1).write_bytes(b"pack1")
        (data_dir / pack_sha2).write_bytes(b"pack2")

        result = _collect_disc_packs(data_dir)
        assert result == {pack_sha1, pack_sha2}

    def test_two_level_layout(self, tmp_path):
        """Collect packs in two-level data/xx/xxxx... layout."""
        data_dir = tmp_path / "data"
        pack_sha = "abcdef1234567890" * 4  # 64 hex chars
        subdir = data_dir / pack_sha[:2]
        subdir.mkdir(parents=True)
        (subdir / pack_sha).write_bytes(b"pack_data")

        result = _collect_disc_packs(data_dir)
        assert result == {pack_sha}

    def test_mixed_flat_and_two_level(self, tmp_path):
        """Handle both flat and two-level packs in same data dir."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        pack_flat = "1" * 64
        (data_dir / pack_flat).write_bytes(b"flat")

        pack_nested = "2" * 64
        subdir = data_dir / pack_nested[:2]
        subdir.mkdir()
        (subdir / pack_nested).write_bytes(b"nested")

        result = _collect_disc_packs(data_dir)
        assert result == {pack_flat, pack_nested}

    def test_skips_non_hex_files(self, tmp_path):
        """Skip files that aren't valid SHA-256 hex."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        valid_pack = "a" * 64
        (data_dir / valid_pack).write_bytes(b"valid")

        (data_dir / "catalog.db").write_bytes(b"not_a_pack")
        (data_dir / "README.txt").write_bytes(b"readme")
        (data_dir / "shortname").write_bytes(b"too_short")

        result = _collect_disc_packs(data_dir)
        assert result == {valid_pack}

    def test_skips_uppercase_hex_chars(self, tmp_path):
        """Pack files with uppercase hex are skipped (lowercase-only check)."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()

        # Lowercase pack (accepted)
        pack_lower = "abcdef" + "0" * 58
        (data_dir / pack_lower).write_bytes(b"lower")

        # Uppercase pack (should be skipped, but ideally should work)
        pack_upper = "ABCDEF" + "0" * 58
        (data_dir / pack_upper).write_bytes(b"upper")

        result = _collect_disc_packs(data_dir)
        # Currently only accepts lowercase
        assert pack_lower in result
        assert pack_upper not in result

    def test_missing_data_dir(self, tmp_path):
        """Missing data/ directory returns empty set."""
        data_dir = tmp_path / "nonexistent"
        result = _collect_disc_packs(data_dir)
        assert result == set()

    def test_empty_data_dir(self, tmp_path):
        """Empty data/ directory returns empty set."""
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        result = _collect_disc_packs(data_dir)
        assert result == set()


class TestValidateDisc:
    @pytest.fixture
    def disc_path(self, tmp_path):
        """Create a mock disc with catalog.db and data/ directory."""
        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()

        # Create a minimal valid catalog.db
        conn = sqlite3.connect(str(disc / "catalog.db"))
        conn.execute("CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE)")
        conn.execute("CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, label TEXT, status TEXT)")
        conn.execute("CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER)")
        conn.commit()
        conn.close()

        yield disc

    def test_single_disc_all_packs_present(self, disc_path):
        """Validate disc where all volume_info packs are present."""
        volume_info = {
            "label": "TEST_VOL",
            "uuid": "test-uuid-1234",
            "media_type": "TEST_TINY",
            "pack_count": 2,
            "sha256_manifest": ["a" * 64, "b" * 64],
        }
        with open(disc_path / "volume_info.json", "w") as f:
            json.dump(volume_info, f)

        # Create matching pack files on disc
        (disc_path / "data" / "aa").mkdir(parents=True)
        (disc_path / "data" / "aa" / ("a" * 64)).write_bytes(b"pack_a")
        (disc_path / "data" / "bb").mkdir(parents=True)
        (disc_path / "data" / "bb" / ("b" * 64)).write_bytes(b"pack_b")

        result = validate_disc(disc_path)

        assert result.volume_label == "TEST_VOL"
        assert result.catalog_pack_count == 2
        assert result.disc_pack_count == 2
        assert result.missing_from_disc == []
        assert result.orphaned_on_disc == []
        assert result.ok

    def test_missing_packs_from_disc(self, disc_path):
        """Detect packs missing from disc."""
        volume_info = {
            "label": "PARTIAL_VOL",
            "sha256_manifest": ["a" * 64, "b" * 64],
        }
        with open(disc_path / "volume_info.json", "w") as f:
            json.dump(volume_info, f)

        # Only create one pack file
        (disc_path / "data" / "aa").mkdir(parents=True)
        (disc_path / "data" / "aa" / ("a" * 64)).write_bytes(b"pack_a")

        result = validate_disc(disc_path)

        assert result.catalog_pack_count == 2
        assert result.disc_pack_count == 1
        assert "b" * 64 in result.missing_from_disc
        assert not result.ok

    def test_orphaned_packs_on_disc(self, disc_path):
        """Detect packs on disc not in catalog."""
        volume_info = {
            "label": "TEST_VOL",
            "sha256_manifest": ["a" * 64],
        }
        with open(disc_path / "volume_info.json", "w") as f:
            json.dump(volume_info, f)

        # Create two packs on disc, but only one in manifest
        (disc_path / "data" / "aa").mkdir(parents=True)
        (disc_path / "data" / "aa" / ("a" * 64)).write_bytes(b"pack_a")
        # Use all lowercase hex for the orphaned pack
        (disc_path / "data" / "99").mkdir(parents=True)
        orphan_hash = "9" * 64
        (disc_path / "data" / "99" / orphan_hash).write_bytes(b"orphan")

        result = validate_disc(disc_path)

        assert result.catalog_pack_count == 1
        assert result.disc_pack_count == 2
        assert orphan_hash in result.orphaned_on_disc
        assert not result.ok

    def test_missing_catalog_db_raises(self, tmp_path):
        """Missing catalog.db raises FileNotFoundError."""
        disc = tmp_path / "disc"
        disc.mkdir()
        (disc / "data").mkdir()

        with pytest.raises(FileNotFoundError, match="catalog.db"):
            validate_disc(disc)

    def test_missing_data_dir_raises(self, disc_path):
        """Missing data/ directory raises ValueError."""
        (disc_path / "data").rmdir()
        with pytest.raises(ValueError, match="data/"):
            validate_disc(disc_path)

    def test_empty_disc_with_volume_info(self, disc_path):
        """Empty disc with empty manifest is valid."""
        volume_info = {"label": "EMPTY_VOL", "sha256_manifest": []}
        with open(disc_path / "volume_info.json", "w") as f:
            json.dump(volume_info, f)

        result = validate_disc(disc_path)

        assert result.catalog_pack_count == 0
        assert result.disc_pack_count == 0
        assert result.ok

    def test_catalog_validation_result_ok_property(self):
        """ok property returns True only when no missing or orphaned packs."""
        result = CatalogValidationResult(
            disc_path=Path("/test"),
            missing_from_disc=[],
            orphaned_on_disc=[]
        )
        assert result.ok

        result.missing_from_disc = ["a" * 64]
        assert not result.ok

        result.missing_from_disc = []
        result.orphaned_on_disc = ["b" * 64]
        assert not result.ok

