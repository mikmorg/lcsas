"""Tests for staging builder and holographic metadata injection."""

from __future__ import annotations

import json
from pathlib import Path

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.models import Pack, Volume
from lcsas.staging.builder import StagingBuilder
from lcsas.staging.metadata import HolographicInjector


class TestStagingBuilder:
    def _make_pack(self, sha256: str, size: int = 100) -> Pack:
        return Pack(
            pack_id=1, sha256=sha256, size_bytes=size,
            repo_id="test", is_pruned=False, created_at="",
        )

    def test_initialize(self, tmp_path):
        root = tmp_path / "staging"
        builder = StagingBuilder(root)
        builder.initialize()
        assert root.is_dir()
        assert (root / "data").is_dir()

    def test_stage_packs_flat_layout(self, tmp_path):
        # Create mirror with flat layout
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        (mirror_data / "aaa").write_bytes(b"content_a")
        (mirror_data / "bbb").write_bytes(b"content_b")

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack("aaa"), self._make_pack("bbb")]
        staged = builder.stage_packs(packs, mirror_data)

        assert staged == 2
        assert (staging_root / "data" / "aaa").exists()
        assert (staging_root / "data" / "bbb").exists()

    def test_stage_packs_two_level_layout(self, tmp_path):
        # Create mirror with two-level layout
        mirror_data = tmp_path / "mirror" / "data"
        (mirror_data / "aa").mkdir(parents=True)
        (mirror_data / "aa" / "aabbcc").write_bytes(b"data")

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack("aabbcc")]
        staged = builder.stage_packs(packs, mirror_data)
        assert staged == 1

    def test_stage_missing_pack_skipped(self, tmp_path):
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack("nonexistent")]
        staged = builder.stage_packs(packs, mirror_data)
        assert staged == 0

    def test_cleanup(self, tmp_path):
        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()
        (staging_root / "data" / "file.bin").write_bytes(b"x")
        builder.cleanup()
        assert not staging_root.exists()


class TestHolographicInjector:
    def test_inject_metadata(self, tmp_mirror, tmp_path):
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        injector = HolographicInjector(staging_root)
        injector.inject_metadata({"test_repo": tmp_mirror})

        meta = staging_root / "metadata" / "test_repo"
        assert (meta / "index").is_dir()
        assert (meta / "snapshots").is_dir()
        assert (meta / "keys").is_dir()
        assert (meta / "config").is_file()

    def test_inject_catalog(self, tmp_path):
        staging_root = tmp_path / "staging"
        staging_root.mkdir()
        db_file = tmp_path / "archive.db"
        db_file.write_text("fake db")

        injector = HolographicInjector(staging_root)
        injector.inject_catalog(db_file)

        assert (staging_root / "catalog.db").read_text() == "fake db"

    def test_write_volume_info(self, tmp_path):
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        vol = Volume(
            volume_id=1, label="TEST_001", uuid="test-uuid-123",
            media_type="TEST_TINY", capacity_bytes=1048576,
            used_bytes=0, location="Home", status="STAGING",
            created_at="2026-01-01", closed_at=None, verified_at=None,
        )

        injector = HolographicInjector(staging_root)
        injector.write_volume_info(vol)

        info_path = staging_root / "volume_info.json"
        assert info_path.exists()
        info = json.loads(info_path.read_text())
        assert info["uuid"] == "test-uuid-123"
        assert info["label"] == "TEST_001"
        assert info["media_type"] == "TEST_TINY"

    def test_write_volume_info_with_packs(self, tmp_path):
        """volume_info.json includes pack_count, total_bytes, repos, manifest."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        vol = Volume(
            volume_id=1, label="TEST_001", uuid="test-uuid-123",
            media_type="TEST_TINY", capacity_bytes=1048576,
            used_bytes=3000, location="Home", status="STAGING",
            created_at="2026-01-01", closed_at=None, verified_at=None,
        )
        packs = [
            Pack(pack_id=1, sha256="aaa111", size_bytes=1000,
                 repo_id="family", is_pruned=False, created_at=""),
            Pack(pack_id=2, sha256="bbb222", size_bytes=2000,
                 repo_id="work", is_pruned=False, created_at=""),
        ]

        injector = HolographicInjector(staging_root)
        injector.write_volume_info(vol, packs=packs)

        info = json.loads((staging_root / "volume_info.json").read_text())
        assert info["pack_count"] == 2
        assert info["total_bytes"] == 3000
        assert info["repositories"] == ["family", "work"]
        assert info["sha256_manifest"] == ["aaa111", "bbb222"]

    def test_write_volume_info_no_packs_omits_manifest(self, tmp_path):
        """When no packs are provided, manifest fields are absent."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        vol = Volume(
            volume_id=1, label="TEST_001", uuid="u",
            media_type="TEST_TINY", capacity_bytes=1048576,
            used_bytes=0, location="Home", status="STAGING",
            created_at="2026-01-01", closed_at=None, verified_at=None,
        )

        injector = HolographicInjector(staging_root)
        injector.write_volume_info(vol)

        info = json.loads((staging_root / "volume_info.json").read_text())
        assert "pack_count" not in info
        assert "sha256_manifest" not in info

    def test_write_restore_instructions(self, tmp_path):
        """RESTORE_INSTRUCTIONS.txt is written to staging root."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        injector = HolographicInjector(staging_root)
        injector.write_restore_instructions()

        txt = (staging_root / "RESTORE_INSTRUCTIONS.txt").read_text()
        assert "LCSAS Data Volume" in txt
        assert "encryption key file" in txt
        assert "rustic" in txt

    def test_restore_instructions_no_placeholder_url(self, tmp_path):
        """RESTORE_INSTRUCTIONS.txt must not contain placeholder URLs."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        injector = HolographicInjector(staging_root)
        injector.write_restore_instructions()

        txt = (staging_root / "RESTORE_INSTRUCTIONS.txt").read_text()
        assert "your-org" not in txt
        assert "github.com/your-org" not in txt

    def test_restore_instructions_get_help_advice(self, tmp_path):
        """RESTORE_INSTRUCTIONS.txt should tell users to seek professional help."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        injector = HolographicInjector(staging_root)
        injector.write_restore_instructions()

        txt = (staging_root / "RESTORE_INSTRUCTIONS.txt").read_text()
        assert "computer professional" in txt

    def test_write_start_here_with_config(self, tmp_path):
        """START_HERE.txt generated from config survivability fields."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
            archive_owner="John Smith",
            archive_description="Family photos and videos 2000-2025",
            key_storage_hints="Paper copy in the home safe",
            technical_contact="Jane Smith (jane@example.com)",
            repositories={
                "family": RepositoryConfig(
                    name="family",
                    mirror_path=tmp_path / "mirror" / "family",
                    password_file=Path("/keys/family.key"),
                ),
            },
        )

        injector = HolographicInjector(staging_root)
        injector.write_start_here(config)

        txt = (staging_root / "START_HERE.txt").read_text()
        assert "John Smith" in txt
        assert "Family photos and videos 2000-2025" in txt
        assert "Paper copy in the home safe" in txt
        assert "Jane Smith" in txt
        assert "START HERE" in txt
        assert "ENCRYPTION KEY" in txt
        assert "family" in txt  # repo name

    def test_write_start_here_defaults(self, tmp_path):
        """START_HERE.txt uses reasonable defaults when config fields are empty."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
        )

        injector = HolographicInjector(staging_root)
        injector.write_start_here(config)

        txt = (staging_root / "START_HERE.txt").read_text()
        assert "START HERE" in txt
        assert "ENCRYPTION KEY" in txt
        assert "computer professional" in txt

    def test_write_key_info_with_repos(self, tmp_path):
        """KEY_INFO.txt lists repos and key file names."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
            key_storage_hints="In the safe deposit box",
            repositories={
                "family": RepositoryConfig(
                    name="family",
                    mirror_path=tmp_path / "mirror" / "family",
                    password_file=Path("/keys/family.key"),
                    encryption_key_id="key-001",
                ),
                "work": RepositoryConfig(
                    name="work",
                    mirror_path=tmp_path / "mirror" / "work",
                ),
            },
        )

        injector = HolographicInjector(staging_root)
        injector.write_key_info(config)

        txt = (staging_root / "KEY_INFO.txt").read_text()
        assert "family" in txt
        assert "work" in txt
        assert "key-001" in txt
        assert "family.key" in txt
        assert "safe deposit box" in txt

    def test_write_key_info_no_repos(self, tmp_path):
        """KEY_INFO.txt handles empty repositories gracefully."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
        )

        injector = HolographicInjector(staging_root)
        injector.write_key_info(config)

        txt = (staging_root / "KEY_INFO.txt").read_text()
        assert "KEY INFORMATION" in txt
        assert "No repositories" in txt

    def test_write_config_summary_with_repos(self, tmp_path):
        """CONFIG_SUMMARY.txt includes config fields and repo names."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
            archive_owner="Alice Smith",
            archive_description="Family archive 2000-2025",
            technical_contact="bob@example.com",
            repositories={
                "photos": RepositoryConfig(
                    name="photos",
                    mirror_path=tmp_path / "mirror" / "photos",
                    password_file=Path("/keys/photos.key"),
                    encryption_key_id="KEY-ABC",
                ),
            },
        )

        injector = HolographicInjector(staging_root)
        injector.write_config_summary(config)

        txt = (staging_root / "CONFIG_SUMMARY.txt").read_text()
        assert "CONFIGURATION SUMMARY" in txt
        assert "Alice Smith" in txt
        assert "Family archive 2000-2025" in txt
        assert "bob@example.com" in txt
        assert "photos" in txt
        assert "KEY-ABC" in txt
        # Filesystem paths should NOT appear
        assert str(tmp_path) not in txt
        assert "Filesystem paths are omitted" in txt

    def test_write_config_summary_minimal(self, tmp_path):
        """CONFIG_SUMMARY.txt handles empty config gracefully."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        config = LCSASConfig(
            mirror_base_path=tmp_path / "mirror",
            staging_path=tmp_path / "staging",
            db_path=tmp_path / "db.db",
        )

        injector = HolographicInjector(staging_root)
        injector.write_config_summary(config)

        txt = (staging_root / "CONFIG_SUMMARY.txt").read_text()
        assert "CONFIGURATION SUMMARY" in txt
        assert "Media type:" in txt
        assert "Filesystem paths are omitted" in txt

    def test_write_disc_care(self, tmp_path):
        """DISC_CARE.txt contains storage guidance."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()

        injector = HolographicInjector(staging_root)
        injector.write_disc_care()

        txt = (staging_root / "DISC_CARE.txt").read_text()
        assert "DISC CARE" in txt
        assert "HANDLING" in txt
        assert "STORAGE" in txt
        assert "ENVIRONMENT" in txt
        assert "15-25" in txt  # temperature range
        assert "M-DISC" in txt
        assert "PERIODIC VERIFICATION" in txt
        assert "DRIVE AVAILABILITY" in txt
