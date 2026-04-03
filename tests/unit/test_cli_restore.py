"""Tests for the 'lcsas restore plan' and 'lcsas restore exec' CLI commands."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.cli.main import build_parser, cmd_restore_exec, cmd_restore_plan
from lcsas.db.connection import get_memory_connection
from lcsas.db.models import Pack
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


def _setup_db_with_packs(
    conn: sqlite3.Connection,
    repo_name: str,
    pack_hashes: list[str],
    volume_label: str = "VOL_001",
) -> list[Pack]:
    """Register packs in the DB and assign them to a volume."""
    register_repo(conn, repo_name, repo_name, f"/mnt/mirror/{repo_name}", "")
    packs = []
    for sha in pack_hashes:
        p = register_pack(conn, sha, 1024, repo_name)
        packs.append(p)

    vol = create_volume(
        conn, volume_label, generate_uuid(), "TEST_TINY",
        1_000_000, "Home_Shelf", "VERIFIED",
    )
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in packs])
    return packs


def _make_args(**kwargs):
    """Build a namespace mimicking parsed CLI args."""
    defaults = {
        "config": Path("config.toml"),  # non-None; load_config is mocked in tests
        "db": None,
        "command": "restore",
        "repo": "family",
        "snapshot_id": "abc123",
        "skip_verify": True,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# Parser tests
# ---------------------------------------------------------------------------


class TestRestoreParser:
    def test_restore_plan_parser(self):
        parser = build_parser()
        args = parser.parse_args(["restore", "plan", "snap123", "--repo", "family"])
        assert args.command == "restore"
        assert args.restore_command == "plan"
        assert args.snapshot_id == "snap123"
        assert args.repo == "family"

    def test_restore_exec_parser(self):
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap123", "/tmp/out",
            "--repo", "family",
            "--password-file", "/root/keys/family.key",
        ])
        assert args.command == "restore"
        assert args.restore_command == "exec"
        assert args.snapshot_id == "snap123"
        assert args.target_path == Path("/tmp/out")
        assert args.repo == "family"
        assert args.password_file == Path("/root/keys/family.key")

    def test_restore_exec_with_volume_dir(self):
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap123", "/tmp/out",
            "--repo", "family",
            "--password-file", "/root/keys/family.key",
            "--volume-dir", "/media/discs",
        ])
        assert args.volume_dir == Path("/media/discs")

    def test_restore_exec_with_cache_dir(self):
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap123", "/tmp/out",
            "--repo", "family",
            "--password-file", "/root/keys/family.key",
            "--cache-dir", "/tmp/cache",
        ])
        assert args.cache_dir == Path("/tmp/cache")


# ---------------------------------------------------------------------------
# cmd_restore_plan tests
# ---------------------------------------------------------------------------


class TestCmdRestorePlan:
    def test_plan_displays_pick_list(self, tmp_path, caplog):
        """restore plan logs volumes and pack counts."""
        import logging
        conn = get_memory_connection()
        create_all(conn)
        hashes = ["aa" * 32, "bb" * 32, "cc" * 32]
        _setup_db_with_packs(conn, "family", hashes, "ARCHIVE_001")

        mock_plan = RestorePlan(
            snapshot_id="snap1",
            required_pack_hashes=hashes,
            total_size_bytes=3072,
            file_count=10,
        )

        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {
            "family": MagicMock(
                mirror_path=Path("/mnt/mirror/family"),
                password_file=Path("/root/keys/family.key"),
            ),
        }

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_args(restore_command="plan", snapshot_id="snap1")

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            caplog.at_level(logging.INFO),
        ):
            result = cmd_restore_plan(args)

        assert result == 0
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "ARCHIVE_001" in all_msgs
        assert "3 packs" in all_msgs
        assert "family" in all_msgs

    def test_plan_unknown_repo(self, caplog):
        """restore plan with unknown repo logs error."""
        import logging
        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {"family": MagicMock()}

        conn = get_memory_connection()
        create_all(conn)

        args = _make_args(restore_command="plan", repo="nonexistent")

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            caplog.at_level(logging.ERROR),
        ):
            result = cmd_restore_plan(args)

        assert result == 1
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "nonexistent" in all_msgs
        assert "not found" in all_msgs

    def test_plan_shows_missing_packs(self, caplog):
        """restore plan errors and returns 1 when packs are not found in any volume."""
        import logging
        conn = get_memory_connection()
        create_all(conn)
        # Register only 1 pack but require 2
        register_repo(conn, "family", "family", "/mnt/mirror/family", "")
        p = register_pack(conn, "aa" * 32, 1024, "family")
        vol = create_volume(
            conn, "VOL_001", generate_uuid(), "TEST_TINY",
            1_000_000, "Home_Shelf", "VERIFIED",
        )
        bulk_link_packs(conn, vol.volume_id, [p.pack_id])

        mock_plan = RestorePlan(
            snapshot_id="snap1",
            required_pack_hashes=["aa" * 32, "ff" * 32],  # ff not in DB
        )

        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {
            "family": MagicMock(
                mirror_path=Path("/mnt/mirror/family"),
                password_file=Path("/root/keys/family.key"),
            ),
        }

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_args(restore_command="plan", snapshot_id="snap1")

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            caplog.at_level(logging.ERROR),
        ):
            result = cmd_restore_plan(args)

        # Missing packs must block restore — return 1, not 0
        assert result == 1
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "not found" in all_msgs
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

    def test_plan_no_packs_needed(self, caplog):
        """restore plan with no required packs logs summary."""
        import logging
        conn = get_memory_connection()
        create_all(conn)
        register_repo(conn, "family", "family", "/mnt/mirror/family", "")

        mock_plan = RestorePlan(
            snapshot_id="snap1",
            required_pack_hashes=[],
        )

        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {
            "family": MagicMock(
                mirror_path=Path("/mnt/mirror/family"),
                password_file=Path("/root/keys/family.key"),
            ),
        }

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_args(restore_command="plan", snapshot_id="snap1")

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            caplog.at_level(logging.INFO),
        ):
            result = cmd_restore_plan(args)

        assert result == 0
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "Required packs: 0" in all_msgs


# ---------------------------------------------------------------------------
# cmd_restore_exec tests
# ---------------------------------------------------------------------------


class TestCmdRestoreExec:
    def test_exec_unknown_repo(self, caplog):
        """restore exec with unknown repo logs error."""
        import logging
        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {}

        conn = get_memory_connection()
        create_all(conn)

        args = _make_args(
            restore_command="exec",
            repo="missing",
            target_path=Path("/tmp/out"),
            password_file=Path("/tmp/key"),
            cache_dir=None,
            volume_dir=None,
        )

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            caplog.at_level(logging.ERROR),
        ):
            result = cmd_restore_exec(args)

        assert result == 1
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "not found" in all_msgs

    def test_exec_fails_on_missing_packs(self, tmp_path, caplog):
        """restore exec aborts if packs are missing from catalog."""
        import logging
        conn = get_memory_connection()
        create_all(conn)
        register_repo(conn, "family", "family", "/mnt/mirror/family", "")

        mock_plan = RestorePlan(
            snapshot_id="snap1",
            required_pack_hashes=["xx" * 32],  # not in any volume
        )

        key_file = tmp_path / "key"
        key_file.write_bytes(b"secret")

        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {
            "family": MagicMock(
                mirror_path=Path("/mnt/mirror/family"),
                password_file=key_file,
            ),
        }

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        args = _make_args(
            restore_command="exec",
            repo="family",
            target_path=Path("/tmp/out"),
            password_file=key_file,
            cache_dir=None,
            volume_dir=None,
        )

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            caplog.at_level(logging.ERROR),
        ):
            result = cmd_restore_exec(args)

        assert result == 1
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "not found in any volume" in all_msgs

    def test_exec_volume_dir_ingests_packs(self, tmp_path, caplog):
        """restore exec with --volume-dir ingests packs and calls restore."""
        import logging
        conn = get_memory_connection()
        create_all(conn)
        hashes = ["aa" * 32, "bb" * 32]
        _setup_db_with_packs(conn, "family", hashes, "VOL_001")

        # Create fake volume directory with pack files
        vol_dir = tmp_path / "volumes" / "VOL_001" / "data"
        vol_dir.mkdir(parents=True)
        for sha in hashes:
            (vol_dir / sha).write_bytes(b"fake_pack_data")

        # Create mirror metadata
        mirror_path = tmp_path / "mirror"
        for sub in ["index", "snapshots", "keys"]:
            (mirror_path / sub).mkdir(parents=True)
        (mirror_path / "config").write_text("{}")

        mock_plan = RestorePlan(
            snapshot_id="snap1",
            required_pack_hashes=hashes,
        )

        key_file = tmp_path / "key"
        key_file.write_bytes(b"secret")

        mock_config = MagicMock()
        mock_config.db_path = Path(":memory:")
        mock_config.repositories = {
            "family": MagicMock(
                mirror_path=mirror_path,
                password_file=key_file,
            ),
        }

        mock_runner = MagicMock()
        mock_runner.restore_dry_run.return_value = mock_plan

        cache_dir = tmp_path / "cache"
        target_dir = tmp_path / "restored"

        args = _make_args(
            restore_command="exec",
            repo="family",
            snapshot_id="snap1",
            target_path=target_dir,
            password_file=key_file,
            cache_dir=cache_dir,
            volume_dir=tmp_path / "volumes",
        )

        with (
            patch("lcsas.config.settings.load_config", return_value=mock_config),
            patch("lcsas.db.connection.get_connection", return_value=conn),
            patch("lcsas.db.schema.create_all"),
            patch("lcsas.rustic.wrapper.SubprocessRusticRunner", return_value=mock_runner),
            caplog.at_level(logging.INFO),
        ):
            result = cmd_restore_exec(args)

        assert result == 0
        all_msgs = " ".join(r.message for r in caplog.records)
        assert "ingested 2 packs" in all_msgs
        assert "Restore complete" in all_msgs

        # Verify rustic restore was called
        mock_runner.restore.assert_called_once()
        call_kwargs = mock_runner.restore.call_args
        assert call_kwargs[1]["snapshot_id"] == "snap1" or call_kwargs[0][0] == "snap1"

    def test_exec_dispatches_via_main(self):
        """restore exec is routed through dispatch()."""
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap1", "/out",
            "--repo", "fam",
            "--password-file", "/key",
        ])
        # Verify dispatch routing exists (command parsed correctly)
        assert args.command == "restore"
        assert args.restore_command == "exec"

    def test_plan_dispatches_via_main(self):
        """restore plan is routed through dispatch()."""
        parser = build_parser()
        args = parser.parse_args(["restore", "plan", "snap1", "--repo", "fam"])
        assert args.command == "restore"
        assert args.restore_command == "plan"


# ---------------------------------------------------------------------------
# _retry_from_alternates_batch helper tests
# ---------------------------------------------------------------------------


class TestRetryFromAlternatesBatch:
    """Unit tests for the _retry_from_alternates_batch helper."""

    def test_missing_dir_emits_warning(self, tmp_path, caplog):
        """When an alternate volume directory does not exist, a warning is logged."""
        import logging

        from lcsas.cli.main import _retry_from_alternates_batch

        vol_dir = tmp_path / "volumes"
        vol_dir.mkdir()
        # Note: vol_dir / "ALT_VOL" does NOT exist

        mock_executor = MagicMock()
        # When falling back to vol_dir, ingest returns (0 ingested, all failed)
        mock_executor.ingest_volume.return_value = (0, ["deadbeef" * 8])
        alternates_map = {"deadbeef" * 8: ["ALT_VOL"]}
        failed_packs = ["deadbeef" * 8]

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            result = _retry_from_alternates_batch(
                mock_executor,
                tmp_path / "cache",
                vol_dir,
                failed_packs,
                alternates_map,
            )

        # The pack should remain unrecovered
        assert "deadbeef" * 8 in result
        # A warning should have been emitted about the missing directory
        assert any("ALT_VOL" in r.message for r in caplog.records)

    def test_no_warning_when_dir_exists(self, tmp_path, caplog):
        """No missing-dir warning when the alternate volume directory exists."""
        import logging

        from lcsas.cli.main import _retry_from_alternates_batch

        vol_dir = tmp_path / "volumes"
        alt_dir = vol_dir / "ALT_VOL"
        alt_dir.mkdir(parents=True)

        mock_executor = MagicMock()
        mock_executor.ingest_volume.return_value = (0, ["deadbeef" * 8])
        alternates_map = {"deadbeef" * 8: ["ALT_VOL"]}
        failed_packs = ["deadbeef" * 8]

        with caplog.at_level(logging.WARNING, logger="lcsas"):
            _retry_from_alternates_batch(
                mock_executor,
                tmp_path / "cache",
                vol_dir,
                failed_packs,
                alternates_map,
            )

        missing_warnings = [
            r for r in caplog.records
            if "not found" in r.message.lower() and "ALT_VOL" in r.message
        ]
        assert not missing_warnings
