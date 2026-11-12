"""Tests for CLI argument parsing and dispatch."""

from __future__ import annotations

import sqlite3

import pytest

from lcsas.cli.main import build_parser, main
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


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

    def test_repo_add_rejects_key_file_flag(self, capsys):
        """The historical ``--key-file`` flag was silently ignored — the
        real encryption key is auto-detected from ``mirror_path/keys/``.
        After its removal (#37) argparse must reject it as unknown so
        operators are not misled into thinking it controls key
        selection.
        """
        with pytest.raises(SystemExit) as excinfo:
            self.parser.parse_args([
                "repo", "add", "--key-file", "/tmp/some.key",
                "family", "/mnt/mirror/fam",
            ])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "unrecognized arguments" in err
        assert "--key-file" in err

    def test_repo_list(self):
        args = self.parser.parse_args(["repo", "list"])
        assert args.command == "repo"
        assert args.repo_command == "list"

    def test_status(self):
        args = self.parser.parse_args(["status"])
        assert args.command == "status"

    def test_burn_with_options(self):
        args = self.parser.parse_args([
            "burn", "--session", "latest", "--media", "TEST_TINY"
        ])
        assert args.command == "burn"
        assert args.media == "TEST_TINY"
        assert args.session == "latest"

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

    def test_no_command_shows_help(self, capsys):
        result = main([])
        assert result == 0



class TestReadOnlyCommandsRequireCatalog:
    """FUP-02 mitigation 4: read-only commands must fail (not create a
    catalog) when none exists, and perform zero writes outside the lock."""

    def test_status_fails_without_catalog(self, tmp_path, capsys):
        db = tmp_path / "nope.db"
        result = main(["--db", str(db), "status"])
        assert result != 0
        combined = capsys.readouterr()
        text = combined.out + combined.err
        assert "lcsas init" in text
        # The read-only command must NOT have created the catalog.
        assert not db.exists(), "status created a catalog as a side effect"


class TestDispatchRouting:
    def test_all_commands_parse(self):
        """Verify parser accepts all known commands without errors."""
        parser = build_parser()
        commands = [
            ["init", "--db-path", "/tmp/x.db"],
            ["repo", "add", "fam", "/mnt"],
            ["repo", "list"],
            ["scan"],
            ["status"],
            ["config", "check"],
            ["staging", "clean"],
            ["stage"],
            ["burn", "--session", "latest"],
            ["burn-iso", "/tmp/t.iso"],
            ["location", "list"],
            ["location", "add", "Home"],
            ["location", "status", "Home"],
            ["location", "move", "VOL", "--from", "A", "--to", "B"],
            ["catalog", "import-receipts", "f.json"],
            ["restore", "plan", "snap1", "--repo", "fam"],
            ["restore", "exec", "snap1", "/tmp", "--repo", "fam",
             "--password-file", "/k"],
            ["consolidate", "1", "2"],
            ["verify", "VOL", "--disc"],
            ["meta", "build", "--output", "/tmp/m"],
        ]
        for cmd in commands:
            args = parser.parse_args(cmd)
            assert args.command is not None, f"Failed to parse: {cmd}"

    def test_dry_run_flag_on_stage(self):
        parser = build_parser()
        args = parser.parse_args(["stage", "--dry-run"])
        assert args.dry_run is True

    def test_dry_run_short_flag_on_burn(self):
        parser = build_parser()
        args = parser.parse_args(["burn", "--session", "latest", "-n"])
        assert args.dry_run is True

    def test_skip_verify_flag(self):
        parser = build_parser()
        args = parser.parse_args([
            "restore", "exec", "snap1", "/tmp",
            "--repo", "fam", "--password-file", "/k", "--skip-verify",
        ])
        assert args.skip_verify is True

    def test_no_snapshots_flag(self):
        parser = build_parser()
        args = parser.parse_args(["scan", "--no-snapshots"])
        assert args.no_snapshots is True


def _mem_conn() -> sqlite3.Connection:
    conn = get_connection(":memory:")
    create_all(conn)
    return conn


class TestResolveRepoNamesToIds:
    """Ensure typo'd repo names raise instead of silently matching all repos."""

    def test_none_input_returns_none(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        assert _resolve_repo_names_to_ids(conn, None) is None

    def test_valid_names_return_ids(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        ids = _resolve_repo_names_to_ids(conn, ["Family"])
        assert ids is not None
        assert len(ids) == 1

    def test_all_invalid_names_raises(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")

        with pytest.raises(ValueError, match="None of the specified repositories"):
            _resolve_repo_names_to_ids(conn, ["typo_name", "bogus"])

    def test_partial_valid_returns_valid_only(self):
        from lcsas.cli.main import _resolve_repo_names_to_ids

        conn = _mem_conn()
        register_repo(conn, "family", "Family", "/mnt/mirror/family")
        register_repo(conn, "work", "Work", "/mnt/mirror/work")

        ids = _resolve_repo_names_to_ids(conn, ["Family", "nonexistent"])
        assert ids is not None
        assert len(ids) == 1
