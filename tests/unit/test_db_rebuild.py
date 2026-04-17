"""Unit tests for database catalog rebuild functionality."""

from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from lcsas.db import rebuild, schema
from lcsas.db.connection import get_connection
from lcsas.db.models import Repository, Volume, Pack


class TestRebuildMerge:
    """Test merging disc catalogs into a master database."""

    def test_merge_simple_volumes(self, tmp_path):
        """Merge a simple set of volumes from a source disc."""
        # Create target DB
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        # Create source DB with one volume
        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)
        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test Repo", "/mnt/mirror"),
        )
        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-001", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        assert result["repositories"] == 1
        assert result["volumes"] == 1

        # Verify data was copied
        vol = target_conn.execute(
            "SELECT label, status FROM volumes WHERE uuid = ?", ("uuid-001",)
        ).fetchone()
        assert vol[0] == "VOL001"
        assert vol[1] == "VERIFIED"

        target_conn.close()

    def test_merge_status_conflict_prefers_higher_quality(self, tmp_path):
        """Status conflict resolution prefers higher-quality (more-verified) status."""
        # Target has BURNED, source has VERIFIED → prefer VERIFIED
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Insert the same volume with different statuses
        target_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "same-uuid", "BD25", 25000000000, "BURNED"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001-VERIFIED", "same-uuid", "BD25", 25000000000, "VERIFIED"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        rebuild._merge_one_disc(target_conn, source_db)

        # Target should now have VERIFIED (higher quality)
        vol = target_conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", ("same-uuid",)
        ).fetchone()
        assert vol[0] == "VERIFIED"

        target_conn.close()

    def test_merge_status_conflict_keeps_better_status(self, tmp_path):
        """Status conflict: if target is VERIFIED, don't downgrade to BURNED."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Target has VERIFIED (better), source has BURNED (worse)
        target_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "same-uuid", "BD25", 25000000000, "VERIFIED"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001-BURNED", "same-uuid", "BD25", 25000000000, "BURNED"),
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        rebuild._merge_one_disc(target_conn, source_db)

        # Target should stay VERIFIED (no downgrade)
        vol = target_conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", ("same-uuid",)
        ).fetchone()
        assert vol[0] == "VERIFIED"

        target_conn.close()

    def test_merge_packs_deduplicates_by_sha256(self, tmp_path):
        """Packs are merged with natural-key deduplication (INSERT OR IGNORE)."""
        target_db = tmp_path / "target.db"
        target_conn = get_connection(target_db)
        schema.create_all(target_conn)

        source_db = tmp_path / "source.db"
        source_conn = get_connection(source_db)
        schema.create_all(source_conn)

        # Create a repo first
        target_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Test", "/mnt/mirror"),
        )
        source_conn.commit()

        # Create packs in both DBs
        pack_sha = "a" * 64  # 64-char SHA-256
        target_conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            (pack_sha, 1000, "repo1"),
        )
        target_conn.commit()

        source_conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, ?, ?)",
            (pack_sha, 1000, "repo1"),  # Same pack
        )
        source_conn.commit()
        source_conn.close()

        # Merge
        result = rebuild._merge_one_disc(target_conn, source_db)

        # Pack should not be duplicated (INSERT OR IGNORE)
        count = target_conn.execute(
            "SELECT COUNT(*) FROM packs WHERE sha256 = ?", (pack_sha,)
        ).fetchone()[0]
        assert count == 1

        target_conn.close()

    def test_rebuild_catalog_skip_missing_disc(self, tmp_path):
        """Skip discs that don't have a catalog.db file."""
        output_db = tmp_path / "master.db"

        # Create a disc directory without catalog.db
        disc_dir = tmp_path / "disc1"
        disc_dir.mkdir()

        result = rebuild.rebuild_catalog([disc_dir], output_db)

        assert result.discs_skipped == 1
        assert result.discs_processed == 0
        assert len(result.errors) == 1
        assert "No catalog.db" in result.errors[0]

    def test_rebuild_catalog_processes_multiple_discs(self, tmp_path):
        """Process multiple discs and merge their catalogs."""
        output_db = tmp_path / "master.db"

        # Create two disc directories with catalogs
        disc1_dir = tmp_path / "disc1"
        disc1_dir.mkdir()
        disc1_cat = disc1_dir / "catalog.db"
        disc1_conn = get_connection(disc1_cat)
        schema.create_all(disc1_conn)
        disc1_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo1", "Repo 1", "/mnt/mirror1"),
        )
        disc1_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL001", "uuid-1", "BD25", 25000000000, "VERIFIED"),
        )
        disc1_conn.commit()
        disc1_conn.close()

        disc2_dir = tmp_path / "disc2"
        disc2_dir.mkdir()
        disc2_cat = disc2_dir / "catalog.db"
        disc2_conn = get_connection(disc2_cat)
        schema.create_all(disc2_conn)
        disc2_conn.execute(
            "INSERT INTO repositories (repo_id, name, mirror_path) VALUES (?, ?, ?)",
            ("repo2", "Repo 2", "/mnt/mirror2"),
        )
        disc2_conn.execute(
            "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, status) "
            "VALUES (?, ?, ?, ?, ?)",
            ("VOL002", "uuid-2", "BD25", 25000000000, "BURNED"),
        )
        disc2_conn.commit()
        disc2_conn.close()

        # Rebuild from both discs
        result = rebuild.rebuild_catalog([disc1_dir, disc2_dir], output_db)

        assert result.discs_processed == 2
        assert result.discs_skipped == 0
        assert result.repositories_merged >= 2
        assert result.volumes_merged >= 2

        # Verify merged data
        output_conn = get_connection(output_db)
        repos = output_conn.execute("SELECT COUNT(*) FROM repositories").fetchone()[0]
        vols = output_conn.execute("SELECT COUNT(*) FROM volumes").fetchone()[0]
        assert repos >= 2
        assert vols >= 2
        output_conn.close()

    def test_rebuild_handles_corrupt_source(self, tmp_path):
        """Handle corrupted source database gracefully."""
        output_db = tmp_path / "master.db"

        disc_dir = tmp_path / "disc"
        disc_dir.mkdir()
        catalog_file = disc_dir / "catalog.db"

        # Create a file that looks like a DB but is truncated/corrupt
        catalog_file.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        result = rebuild.rebuild_catalog([disc_dir], output_db)

        assert result.discs_skipped == 1
        assert len(result.errors) == 1
