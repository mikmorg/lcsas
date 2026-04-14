"""Tests for CLI argument parsing and dispatch."""

from __future__ import annotations

import pytest

from lcsas.cli.main import build_parser, main


class TestCLIParsing:
    def setup_method(self):
        self.parser = build_parser()

    def test_version(self, capsys):
        with pytest.raises(SystemExit, match="0"):
            self.parser.parse_args(["--version"])

    def test_init_command(self):
        args = self.parser.parse_args(["init", "--db-path", "/tmp/test.db"])
        assert args.command == "init"

    def test_repo_add(self):
        args = self.parser.parse_args(["repo", "add", "family", "/mnt/mirror/fam"])
        assert args.command == "repo"
        assert args.repo_command == "add"
        assert args.name == "family"

    def test_repo_list(self):
        args = self.parser.parse_args(["repo", "list"])
        assert args.command == "repo"
        assert args.repo_command == "list"

    def test_status(self):
        args = self.parser.parse_args(["status"])
        assert args.command == "status"

    def test_burn_with_options(self):
        args = self.parser.parse_args([
            "burn", "--media", "TEST_TINY", "--skip-ecc"
        ])
        assert args.command == "burn"
        assert args.media == "TEST_TINY"
        assert args.skip_ecc is True

    def test_restore_plan(self):
        args = self.parser.parse_args(["restore", "plan", "snap123", "--repo", "family"])
        assert args.command == "restore"
        assert args.restore_command == "plan"
        assert args.snapshot_id == "snap123"
        assert args.repo == "family"

    def test_consolidate(self):
        args = self.parser.parse_args([
            "consolidate", "1", "2", "3", "--target-media", "MDISC100"
        ])
        assert args.command == "consolidate"
        assert args.volume_ids == [1, 2, 3]
        assert args.target_media == "MDISC100"

    def test_db_export(self):
        args = self.parser.parse_args(["db", "export"])
        assert args.command == "db"
        assert args.db_command == "export"


class TestCLIInit:
    def test_init_creates_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        result = main(["init", "--db-path", str(db_path)])
        assert result == 0
        assert db_path.exists()

    def test_no_command_shows_help(self, capsys):
        result = main([])
        assert result == 0
