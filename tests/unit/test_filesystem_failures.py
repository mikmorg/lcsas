"""Tests for filesystem failure scenarios — disk full, permission denied, OSError."""

from __future__ import annotations

import errno
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.staging.builder import MissingPacksError, StagingBuilder
from lcsas.utils.fs import hardlink_or_copy

# ---------------------------------------------------------------------------
# Staging builder — disk-full and permission failures
# ---------------------------------------------------------------------------

class TestStagingBuilderFilesystemFailures:
    def _make_pack(self, tmp_path: Path, sha256: str, size: int = 1024):
        """Create a fake pack file in a mirror data dir."""
        data_dir = tmp_path / "mirror" / "data"
        prefix = sha256[:2]
        pack_dir = data_dir / prefix
        pack_dir.mkdir(parents=True, exist_ok=True)
        pack_file = pack_dir / sha256
        pack_file.write_bytes(b"\x00" * size)
        return data_dir

    def _make_db_pack(self, sha256: str, size: int = 1024):
        """Return a minimal Pack-like object."""
        from lcsas.db.models import Pack
        return Pack(
            pack_id=1,
            sha256=sha256,
            size_bytes=size,
            repo_id="test",
            is_pruned=False,
            created_at="2026-01-01",
        )

    def test_permission_denied_on_link_adds_to_missing(self, tmp_path):
        """EPERM from os.link causes the pack to be added to missing list."""
        sha = "a" * 64
        data_dir = self._make_pack(tmp_path, sha)
        pack = self._make_db_pack(sha)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        perm_err = OSError(errno.EPERM, "Operation not permitted")
        with (
            patch("lcsas.utils.fs.os.link", side_effect=perm_err),
            pytest.raises(MissingPacksError) as exc_info,
        ):
            builder.stage_packs([pack], data_dir)

        assert sha[:12] in exc_info.value.missing

    def test_disk_full_on_copy_fallback_adds_to_missing(self, tmp_path):
        """ENOSPC during the EXDEV copy fallback causes pack to be listed as missing."""
        sha = "b" * 64
        data_dir = self._make_pack(tmp_path, sha)
        pack = self._make_db_pack(sha)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        exdev_err = OSError(errno.EXDEV, "Cross-device link")
        nospc_err = OSError(errno.ENOSPC, "No space left on device")

        with (
            patch("lcsas.utils.fs.os.link", side_effect=exdev_err),
            patch("lcsas.utils.fs.shutil.copy2", side_effect=nospc_err),
            pytest.raises((MissingPacksError, OSError)),
        ):
            builder.stage_packs([pack], data_dir)

    def test_symlink_source_rejected(self, tmp_path):
        """A symlinked pack file is rejected (possible path injection)."""
        sha = "c" * 64
        data_dir = self._make_pack(tmp_path, sha)
        pack = self._make_db_pack(sha)

        # Replace the real file with a symlink
        real = data_dir / sha[:2] / sha
        link_target = tmp_path / "other_file"
        link_target.write_bytes(b"other")
        real.unlink()
        real.symlink_to(link_target)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        with pytest.raises(MissingPacksError) as exc_info:
            builder.stage_packs([pack], data_dir)

        assert sha[:12] in exc_info.value.missing

    def test_empty_staged_file_detected(self, tmp_path):
        """A staged file with zero bytes is treated as missing."""
        sha = "d" * 64
        data_dir = self._make_pack(tmp_path, sha)
        pack = self._make_db_pack(sha)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        # Simulate hardlink succeeding but producing an empty file
        def _empty_link(src, dst):
            Path(dst).write_bytes(b"")

        with (
            patch("lcsas.utils.fs.os.link", side_effect=_empty_link),
            pytest.raises(MissingPacksError) as exc_info,
        ):
            builder.stage_packs([pack], data_dir)

        assert sha[:12] in exc_info.value.missing

    def test_multiple_packs_all_failures_collected(self, tmp_path):
        """All missing packs are reported, not just the first failure."""
        from lcsas.db.models import Pack

        data_dir = tmp_path / "mirror" / "data"
        data_dir.mkdir(parents=True)

        packs = [
            Pack(pack_id=i, sha256=str(i) * 64, size_bytes=100,
                 repo_id="test", is_pruned=False, created_at="2026-01-01")
            for i in range(1, 4)
        ]

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        # None of the packs exist in the mirror
        with pytest.raises(MissingPacksError) as exc_info:
            builder.stage_packs(packs, data_dir)

        assert len(exc_info.value.missing) == 3

    def test_already_staged_pack_skipped(self, tmp_path):
        """Packs already present in staging are counted as staged (resume support)."""
        from lcsas.utils.hashing import sha256_bytes
        # Use actual SHA-256 of 1024 zero bytes
        content = b"\x00" * 1024
        sha = sha256_bytes(content)
        data_dir = self._make_pack(tmp_path, sha)
        pack = self._make_db_pack(sha)

        staging_root = tmp_path / "staging"
        builder = StagingBuilder(staging_root)
        builder.initialize()

        # Pre-stage the pack
        dst = staging_root / "data" / sha[:2] / sha
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(content)

        # Should succeed without calling os.link at all
        with patch("lcsas.utils.fs.os.link") as mock_link:
            count = builder.stage_packs([pack], data_dir)
            mock_link.assert_not_called()

        assert count == 1


# ---------------------------------------------------------------------------
# hardlink_or_copy — non-EXDEV errors
# ---------------------------------------------------------------------------

class TestHardlinkOrCopyErrors:
    def test_enospc_is_not_swallowed(self, tmp_path):
        """ENOSPC from os.link is re-raised (not silently ignored)."""
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "dst.txt"

        nospc_err = OSError(errno.ENOSPC, "No space left on device")
        with (
            patch("lcsas.utils.fs.os.link", side_effect=nospc_err),
            pytest.raises(OSError) as exc_info,
        ):
            hardlink_or_copy(src, dst)

        assert exc_info.value.errno == errno.ENOSPC

    def test_eperm_is_not_swallowed(self, tmp_path):
        """EPERM from os.link is re-raised."""
        src = tmp_path / "src.txt"
        src.write_text("content")
        dst = tmp_path / "dst.txt"

        perm_err = OSError(errno.EPERM, "Operation not permitted")
        with (
            patch("lcsas.utils.fs.os.link", side_effect=perm_err),
            pytest.raises(OSError) as exc_info,
        ):
            hardlink_or_copy(src, dst)

        assert exc_info.value.errno == errno.EPERM

    def test_exdev_falls_back_to_copy(self, tmp_path):
        """EXDEV from os.link triggers shutil.copy2 fallback."""
        src = tmp_path / "src.txt"
        src.write_text("cross-device content")
        dst = tmp_path / "dst.txt"

        exdev_err = OSError(errno.EXDEV, "Cross-device link")
        with patch("lcsas.utils.fs.os.link", side_effect=exdev_err):
            hardlink_or_copy(src, dst)

        assert dst.read_text() == "cross-device content"


# ---------------------------------------------------------------------------
# DVDisaster — disk space check
# ---------------------------------------------------------------------------

class TestDVDisasterDiskSpace:
    def test_augment_raises_on_insufficient_space(self, tmp_path):
        """augment_iso raises OSError before calling subprocess when disk is full."""
        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner

        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=0),
            ),
            pytest.raises(OSError, match="Insufficient disk space"),
        ):
            runner.augment_iso(iso)

    def test_augment_proceeds_with_sufficient_space(self, tmp_path):
        """augment_iso calls dvdisaster when there is enough free space."""
        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner

        runner = SubprocessDVDisasterRunner()
        iso = tmp_path / "test.iso"
        iso.write_bytes(b"\x00" * 1024)

        with (
            patch(
                "lcsas.ecc.dvdisaster.shutil.disk_usage",
                return_value=MagicMock(free=10_737_418_240),  # 10 GiB
            ),
            patch("lcsas.ecc.dvdisaster.subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            runner.augment_iso(iso)

        mock_run.assert_called_once()
