"""Tests for staging builder and holographic metadata injection."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.models import Pack, Volume
from lcsas.staging.builder import (
    CorruptPacksError,
    MirrorUnavailableError,
    MissingPacksError,
    StagingBuilder,
)
from lcsas.staging.metadata import HolographicInjector


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


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
        content_a, content_b = b"content_a", b"content_b"
        sha_a, sha_b = _sha(content_a), _sha(content_b)
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        (mirror_data / sha_a).write_bytes(content_a)
        (mirror_data / sha_b).write_bytes(content_b)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [
            self._make_pack(sha_a, size=len(content_a)),
            self._make_pack(sha_b, size=len(content_b)),
        ]
        staged = builder.stage_packs(packs, mirror_data)

        assert staged == 2
        # Two-level layout on staging: data/<prefix>/<hash>
        assert (staging_root / "data" / sha_a[:2] / sha_a).exists()
        assert (staging_root / "data" / sha_b[:2] / sha_b).exists()

    def test_stage_packs_two_level_layout(self, tmp_path):
        # Create mirror with two-level layout
        content = b"data"
        sha = _sha(content)
        mirror_data = tmp_path / "mirror" / "data"
        (mirror_data / sha[:2]).mkdir(parents=True)
        (mirror_data / sha[:2] / sha).write_bytes(content)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack(sha, size=len(content))]
        staged = builder.stage_packs(packs, mirror_data)
        assert staged == 1

    def test_stage_missing_pack_raises(self, tmp_path):
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack("nonexistent")]
        with pytest.raises(MissingPacksError, match="nonexistent"):
            builder.stage_packs(packs, mirror_data)

    def test_symlink_pack_treated_as_missing(self, tmp_path):
        """Symlink pack files are rejected to prevent path injection attacks."""
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)

        # Create a real file and a symlink pointing somewhere else
        target = tmp_path / "outside_mirror_file.bin"
        target.write_bytes(b"secret_content")
        symlink = mirror_data / "aabbcc"
        symlink.symlink_to(target)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        packs = [self._make_pack("aabbcc")]
        with pytest.raises(MissingPacksError, match="aabbcc"):
            builder.stage_packs(packs, mirror_data)

        # Destination should not have been created
        assert not (staging_root / "data" / "aa" / "aabbcc").exists()

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

    def test_inject_metadata_raises_when_repo_config_missing(self, tmp_path):
        """BURN-01: a required repo whose mirror lacks config+keys fails loud.

        Repos with packs on the volume must keep the disc self-describing;
        an unmounted/bare mirror root raises instead of silently injecting
        nothing.
        """
        staging_root = tmp_path / "staging"
        staging_root.mkdir()
        bare_mirror = tmp_path / "bare_mirror"
        bare_mirror.mkdir()  # exists, but has neither config nor keys/

        injector = HolographicInjector(staging_root)
        with pytest.raises(MirrorUnavailableError, match="test_repo"):
            injector.inject_metadata(
                {"test_repo": bare_mirror}, required_repos={"test_repo"}
            )

    def test_inject_metadata_non_required_repo_still_skipped(self, tmp_path):
        """Repos without packs on this volume keep the lenient skip."""
        staging_root = tmp_path / "staging"
        staging_root.mkdir()
        bare_mirror = tmp_path / "bare_mirror"
        bare_mirror.mkdir()

        injector = HolographicInjector(staging_root)
        injector.inject_metadata({"test_repo": bare_mirror})  # must not raise
        assert (staging_root / "metadata" / "test_repo").is_dir()

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


class TestPartialStagedPack:
    """Test for T17: partial staging file (non-zero, wrong size) re-stages (R3-M6)."""

    def _make_pack(self, sha256: str, size: int) -> Pack:
        return Pack(
            pack_id=1, sha256=sha256, size_bytes=size,
            repo_id="test", is_pruned=False, created_at="",
        )

    def test_partial_staged_pack_re_stages(self, tmp_path):
        """Partially-written pack (wrong size) is re-staged from source."""
        # Create mirror with full pack
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        full_content = b"x" * 1000
        sha = _sha(full_content)
        (mirror_data / sha).write_bytes(full_content)

        # Create staging with partial file (half size)
        staging_root = tmp_path / "staging"
        staging_root.mkdir()
        builder = StagingBuilder(staging_root)
        builder.initialize()

        prefix_dir = staging_root / "data" / sha[:2]
        prefix_dir.mkdir(parents=True, exist_ok=True)
        partial_path = prefix_dir / sha
        partial_path.write_bytes(b"x" * 500)  # Only half the expected size

        # Stage the pack (should detect partial and re-stage)
        pack = self._make_pack(sha, size=1000)
        staged = builder.stage_packs([pack], mirror_data)

        assert staged == 1
        # File should now be full size
        assert partial_path.stat().st_size == 1000
        assert partial_path.read_bytes() == full_content


class TestStagePackContentVerification:
    """BURN-02: every staged pack's bytes must hash to its catalog SHA-256.

    The dst is a hardlink to the mirror inode, so the staging-time hash
    verifies the actual mirror bytes — the only hot copy — before they
    are replicated to every disc at every location.
    """

    def _make_pack(self, sha256: str, size: int) -> Pack:
        return Pack(
            pack_id=1, sha256=sha256, size_bytes=size,
            repo_id="test", is_pruned=False, created_at="",
        )

    def test_stage_rejects_pack_with_corrupt_content(self, tmp_path):
        """A mirror pack whose bytes don't hash to its filename aborts staging."""
        good_content = b"the bytes rustic originally wrote"
        sha = _sha(good_content)
        rotted = b"the bytes after silent NAS bit-rot!"  # same name, wrong bytes

        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        (mirror_data / sha).write_bytes(rotted)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        pack = self._make_pack(sha, size=len(rotted))
        with pytest.raises(CorruptPacksError, match="CORRUPT") as excinfo:
            builder.stage_packs([pack], mirror_data)

        assert excinfo.value.corrupt == [(sha, _sha(rotted))]
        # The corrupt file must not linger in the staging tree.
        assert not (staging_root / "data" / sha[:2] / sha).exists()

    def test_fresh_hardlink_is_hash_verified(self, tmp_path):
        """Fresh stage of a corrupt source raises; good siblings still staged."""
        good = b"valid pack content"
        good_sha = _sha(good)
        bad = b"corrupt pack content"
        bad_sha = _sha(b"what the corrupt pack SHOULD contain")

        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        (mirror_data / good_sha).write_bytes(good)
        (mirror_data / bad_sha).write_bytes(bad)

        builder = StagingBuilder(tmp_path / "staging")
        builder.initialize()

        packs = [
            self._make_pack(good_sha, size=len(good)),
            self._make_pack(bad_sha, size=len(bad)),
        ]
        with pytest.raises(CorruptPacksError, match=bad_sha[:12]):
            builder.stage_packs(packs, mirror_data)

    def test_preexisting_large_staged_pack_is_hash_verified(self, tmp_path, monkeypatch):
        """Pre-existing dst is hash-checked at ANY size (the 500 MB skip is gone).

        Uses a sparse 600 MB file (above the deleted threshold) and a mock
        on ``sha256_file`` to pin that the content check actually runs.
        """
        size = 600_000_000  # > the removed 500_000_000 threshold
        sha = "c" * 64

        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        with open(mirror_data / sha, "wb") as f:
            f.truncate(size)  # sparse — no real disk usage

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        dst = staging_root / "data" / sha[:2] / sha
        dst.parent.mkdir(parents=True)
        with open(dst, "wb") as f:
            f.truncate(size)  # pre-existing dst with the correct size

        mock_hash = MagicMock(return_value=sha)
        monkeypatch.setattr("lcsas.staging.builder.sha256_file", mock_hash)

        pack = self._make_pack(sha, size=size)
        staged = builder.stage_packs([pack], mirror_data)

        assert staged == 1
        mock_hash.assert_called_once()
        assert mock_hash.call_args[0][0] == dst

    def test_happy_path_hashes_each_pack_exactly_once(self, tmp_path, monkeypatch):
        """Acceptance: every staged pack is read+hashed exactly once."""
        from lcsas.utils.hashing import sha256_file as real_sha256_file

        contents = [b"pack one content", b"pack two content", b"pack three"]
        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        packs = []
        for c in contents:
            sha = _sha(c)
            (mirror_data / sha).write_bytes(c)
            packs.append(self._make_pack(sha, size=len(c)))

        builder = StagingBuilder(tmp_path / "staging")
        builder.initialize()

        mock_hash = MagicMock(side_effect=real_sha256_file)
        monkeypatch.setattr("lcsas.staging.builder.sha256_file", mock_hash)

        staged = builder.stage_packs(packs, mirror_data)

        assert staged == len(contents)
        assert mock_hash.call_count == len(contents)

    def test_corrupt_preexisting_dst_restaged_from_good_source(self, tmp_path):
        """A bit-rotted leftover in staging is re-staged from a good mirror copy."""
        content = b"y" * 1000
        sha = _sha(content)

        mirror_data = tmp_path / "mirror" / "data"
        mirror_data.mkdir(parents=True)
        (mirror_data / sha).write_bytes(content)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        dst = staging_root / "data" / sha[:2] / sha
        dst.parent.mkdir(parents=True)
        dst.write_bytes(b"z" * 1000)  # right size, wrong bytes

        pack = self._make_pack(sha, size=1000)
        staged = builder.stage_packs([pack], mirror_data)

        assert staged == 1
        assert dst.read_bytes() == content
