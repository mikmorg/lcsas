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
    def test_unimplemented_command(self, capsys):
        """Unimplemented commands return 1."""
        result = main(["burn"])
        assert result == 1

    def test_verify_not_implemented(self, capsys):
        result = main(["verify", "SOME_LABEL"])
        assert result == 1

    def test_dispatch_error_handling(self, tmp_path, capsys):
        """Exception in dispatch is caught and returns 1."""
        # Use a non-existent DB path that will fail.
        # The exact error message varies by OS/SQLite version, but it should
        # be an error (return code 1) and print something to stdout.
        result = main(["--db", "/nonexistent/path/db.sqlite", "status"])
        assert result == 1
        out = capsys.readouterr().out
        # Accept either SQLite "unable to open" or system "Permission denied"
        assert any(phrase in out for phrase in [
            "unable to open database file",
            "Permission denied",
            "Errno 13",
        ])
