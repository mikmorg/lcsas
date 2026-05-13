"""Tests for CLI command handlers: repo add/list, status."""

from __future__ import annotations

from lcsas.cli.main import main


class TestCmdRepoAdd:
    def test_repo_add_registers(self, tmp_path, capsys):
        """repo add creates DB and registers repo."""
        db = tmp_path / "test.db"
        # First init
        assert main(["init", "--db-path", str(db)]) == 0

        result = main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        assert result == 0
        out = capsys.readouterr().out
        assert "Registered repository 'family'" in out

    def test_repo_add_without_init(self, tmp_path, capsys):
        """repo add auto-initializes DB."""
        db = tmp_path / "new.db"
        result = main(["--db", str(db), "repo", "add", "work", str(tmp_path)])
        assert result == 0
        out = capsys.readouterr().out
        assert "Registered repository 'work'" in out


class TestCmdRepoList:
    def test_repo_list_empty(self, tmp_path, capsys):
        """Empty repo list prints message."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "No repositories registered" in out

    def test_repo_list_populated(self, tmp_path, capsys):
        """Lists registered repos with IDs."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", "/mnt/mirror/family"])
        main(["--db", str(db), "repo", "add", "work", "/mnt/mirror/work"])
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        assert "family" in out
        assert "work" in out


class TestCmdStatus:
    def test_status_empty_db(self, tmp_path, capsys):
        """Status on empty DB shows all zeros."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "0 total" in out
        assert "Volumes: 0" in out

    def test_status_with_data(self, tmp_path, capsys):
        """Status with repos shows counts."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "fam", "/mnt/fam"])
        capsys.readouterr()

        result = main(["--db", str(db), "status"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Packs:" in out


class TestCmdDispatchEdges:
    def test_burn_requires_session(self, capsys):
        """`lcsas burn` without `--session` fails fast via argparse.

        Regression test for #60: the legacy stage+burn handler was
        removed, so `--session` is now required and argparse should
        reject the bare `burn` invocation with a non-zero exit code and
        a message referencing `--session`.
        """
        import pytest

        with pytest.raises(SystemExit) as excinfo:
            main(["burn"])
        assert excinfo.value.code != 0
        err = capsys.readouterr().err
        assert "--session" in err

    def test_verify_not_implemented(self, capsys):
        result = main(["verify", "SOME_LABEL"])
        assert result == 1

    def test_status_auto_creates_db_at_unknown_path(self, tmp_path, capsys):
        """`status` against a fresh DB path auto-creates the file and schema.

        Regression test for the auto-init path: ``cmd_status`` defensively
        calls ``create_all()`` and ``get_connection`` creates missing parent
        directories, so an unused path should succeed (not error out).
        """
        import sqlite3

        db = tmp_path / "fresh-subdir" / "archive.db"
        assert not db.exists()
        assert not db.parent.exists()

        result = main(["--db", str(db), "status"])
        assert result == 0
        assert db.exists(), "status should have created the DB file"

        # Verify the schema was applied (a few core tables should exist).
        conn = sqlite3.connect(str(db))
        try:
            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        finally:
            conn.close()
        for expected in ("repositories", "packs", "volumes"):
            assert expected in tables, f"expected table '{expected}' in {tables}"
