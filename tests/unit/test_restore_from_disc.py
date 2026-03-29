"""Tests for the 'lcsas restore from-disc' CLI command."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.cli.main import build_parser, cmd_restore_from_disc
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume
from lcsas.rustic.types import RestorePlan
from lcsas.utils.labels import generate_uuid

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_disc_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace mimicking parsed 'restore from-disc' args."""
    defaults = {
        "command": "restore",
        "restore_command": "from-disc",
        "disc": Path("/mnt/disc1"),
        "target_path": Path("/tmp/restored"),
        "password_file": Path("/home/user/secret.key"),
        "repo": None,
        "snapshot": "latest",
        "volume_dir": None,
        "catalog": None,
        "cache_dir": None,
        "skip_verify": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


def _write_catalog(path: Path, repo_name: str = "family") -> None:
    """Write a minimal catalog.db to *path* with one repository."""
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    conn.commit()
    conn.close()


def _write_catalog_with_packs(
    path: Path,
    repo_name: str = "family",
    pack_hashes: list[str] | None = None,
    volume_label: str = "VOL_001",
) -> list[str]:
    """Write a catalog.db with packs assigned to a volume."""
    if pack_hashes is None:
        pack_hashes = ["aa" * 32, "bb" * 32]
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    create_all(conn)
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    packs = [register_pack(conn, sha, 1024, repo_name) for sha in pack_hashes]
    vol = create_volume(
        conn, volume_label, generate_uuid(), "TEST_TINY",
        1_000_000, "Home_Shelf", "VERIFIED",
    )
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    conn.commit()
    conn.close()
    return pack_hashes


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestFromDiscParser:
    def test_from_disc_subcommand_registered(self):
        """Parser accepts 'restore from-disc' without error."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.command == "restore"
        assert args.restore_command == "from-disc"
        assert args.disc == Path("/mnt/disc1")
        assert args.target_path == Path("/tmp/out")
        assert args.password_file == Path("/home/user/secret.key")

    def test_from_disc_snapshot_default(self):
        """--snapshot defaults to 'latest'."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
        ])
        assert args.snapshot == "latest"

    def test_from_disc_all_optional_flags(self):
        """All optional flags are accepted."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "from-disc",
            "/mnt/disc1", "/tmp/out",
            "--password-file", "/home/user/secret.key",
            "--repo", "family",
            "--snapshot", "abc123def",
            "--volume-dir", "/media/vols",
            "--cache-dir", "/tmp/cache",
            "--skip-verify",
        ])
        assert args.repo == "family"
        assert args.snapshot == "abc123def"
        assert args.volume_dir == Path("/media/vols")
        assert args.cache_dir == Path("/tmp/cache")
        assert args.skip_verify is True


# ---------------------------------------------------------------------------
# Validation / early-exit tests
# ---------------------------------------------------------------------------


class TestFromDiscValidation:
    def test_disc_path_not_dir_returns_1(self, tmp_path):
        """Non-directory disc path returns exit code 1."""
        f = tmp_path / "notadir"
        f.write_text("x")
        args = _make_disc_args(disc=f)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_no_catalog_db_returns_1(self, tmp_path):
        """Missing catalog.db returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_no_repositories_returns_1(self, tmp_path):
        """A catalog with no repositories returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        # Create schema but don't insert any repos
        conn = sqlite3.connect(str(catalog))
        create_all(conn)
        conn.commit()
        conn.close()
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_multiple_repos_without_flag_returns_1(self, tmp_path):
        """Multiple repos without --repo returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        conn = sqlite3.connect(str(catalog))
        conn.row_factory = sqlite3.Row
        create_all(conn)
        register_repo(conn, "family", "family", "/mnt/mirror/family", "")
        register_repo(conn, "work", "work", "/mnt/mirror/work", "")
        conn.commit()
        conn.close()
        args = _make_disc_args(disc=disc, repo=None)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_unknown_repo_name_returns_1(self, tmp_path):
        """--repo with a name not in the catalog returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        args = _make_disc_args(disc=disc, repo="nonexistent")
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_missing_metadata_dir_returns_1(self, tmp_path):
        """Missing metadata/<repo_name>/ on disc returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        # No metadata/ directory created
        args = _make_disc_args(disc=disc)
        result = cmd_restore_from_disc(args)
        assert result == 1

    def test_rustic_not_found_returns_1(self, tmp_path):
        """FileNotFoundError from restore_dry_run returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        _write_catalog(catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.side_effect = FileNotFoundError("rustic not found")
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None

        args = _make_disc_args(disc=disc)
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 1

    def test_interactive_no_tty_returns_1(self, tmp_path):
        """Non-interactive stdin returns exit code 1 in interactive mode."""
        disc = tmp_path / "disc"
        disc.mkdir()
        catalog = disc / "catalog.db"
        pack_hashes = _write_catalog_with_packs(catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = (0, [])
        mock_executor.verify_cache_completeness = MagicMock(return_value=pack_hashes)

        args = _make_disc_args(disc=disc, volume_dir=None, skip_verify=True)
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=pack_hashes),
            patch("sys.stdin") as mock_stdin,
        ):
            mock_stdin.isatty.return_value = False
            result = cmd_restore_from_disc(args)
        assert result == 1


# ---------------------------------------------------------------------------
# Happy-path: batch mode with --volume-dir
# ---------------------------------------------------------------------------


class TestFromDiscBatchMode:
    def test_batch_restore_returns_0(self, tmp_path):
        """Batch restore with all packs available returns exit code 0."""
        disc = tmp_path / "disc"
        disc.mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = (len(pack_hashes), [])

        target = tmp_path / "restored"

        args = _make_disc_args(
            disc=disc,
            target_path=target,
            volume_dir=vol_dir,
            skip_verify=True,
        )
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0
        mock_executor.execute_restore.assert_called_once()

    def test_batch_missing_packs_returns_1(self, tmp_path):
        """Batch restore with permanently missing packs returns exit code 1."""
        disc = tmp_path / "disc"
        disc.mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = (0, [])

        args = _make_disc_args(
            disc=disc,
            target_path=tmp_path / "restored",
            volume_dir=vol_dir,
            skip_verify=True,
        )
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=pack_hashes),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 1
        mock_executor.execute_restore.assert_not_called()

    def test_single_repo_auto_selected(self, tmp_path):
        """Single repo in catalog is auto-selected without --repo flag."""
        disc = tmp_path / "disc"
        disc.mkdir()
        pack_hashes = _write_catalog_with_packs(
            disc / "catalog.db", repo_name="family"
        )
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = (len(pack_hashes), [])

        args = _make_disc_args(
            disc=disc,
            repo=None,  # no explicit --repo
            volume_dir=vol_dir,
            skip_verify=True,
        )
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0

    def test_custom_catalog_path_used(self, tmp_path):
        """--catalog overrides default catalog.db location on disc."""
        disc = tmp_path / "disc"
        disc.mkdir()
        custom_catalog = tmp_path / "custom_catalog.db"
        pack_hashes = _write_catalog_with_packs(custom_catalog, repo_name="family")
        meta = disc / "metadata" / "family"
        meta.mkdir(parents=True)

        vol_dir = tmp_path / "vols"
        vol_dir.mkdir()

        mock_plan = MagicMock(spec=RestorePlan)
        mock_plan.required_pack_hashes = pack_hashes
        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan
        mock_executor = MagicMock()
        mock_executor.prepare_cache.return_value = None
        mock_executor.ingest_volume.return_value = (len(pack_hashes), [])

        args = _make_disc_args(
            disc=disc,
            catalog=custom_catalog,
            volume_dir=vol_dir,
            skip_verify=True,
        )
        with (
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            patch("lcsas.restore.executor.RestoreExecutor", return_value=mock_executor),
            patch("lcsas.restore.executor.RestoreExecutor.verify_cache_completeness",
                  return_value=[]),
        ):
            result = cmd_restore_from_disc(args)
        assert result == 0
