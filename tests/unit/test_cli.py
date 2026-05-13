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
            "burn", "--media", "TEST_TINY"
        ])
        assert args.command == "burn"
        assert args.media == "TEST_TINY"

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


class TestCLIInit:
    def test_init_creates_db(self, tmp_path):
        db_path = tmp_path / "test.db"
        result = main(["init", "--db-path", str(db_path)])
        assert result == 0
        assert db_path.exists()

    def test_no_command_shows_help(self, capsys):
        result = main([])
        assert result == 0

    def test_init_honors_config_flag(self, tmp_path, monkeypatch):
        """`lcsas --config <path> init` must create the catalog DB at the
        path declared in the TOML config, not the default ``archive.db``.

        Regression test for issue #17.
        """
        # Run in an isolated cwd so any default `./archive.db` would be
        # easy to detect (and must NOT appear).
        workdir = tmp_path / "cwd"
        workdir.mkdir()
        monkeypatch.chdir(workdir)

        # Custom DB location declared inside the TOML config.
        custom_db = tmp_path / "custom" / "catalog.db"
        mirror = tmp_path / "mirror"
        mirror.mkdir()
        staging = tmp_path / "staging"
        staging.mkdir()

        cfg_path = tmp_path / "lcsas.toml"
        cfg_path.write_text(
            f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{custom_db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
"""
        )

        result = main(["--config", str(cfg_path), "init"])
        assert result == 0

        # The catalog DB must land at the path the TOML config specified.
        assert custom_db.exists(), (
            f"expected catalog DB at {custom_db}, not found"
        )

        # And the default location must NOT have been written.
        assert not (workdir / "archive.db").exists(), (
            "init silently fell back to default archive.db in cwd; "
            "--config was ignored"
        )
