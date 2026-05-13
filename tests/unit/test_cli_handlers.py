"""Tests for CLI command handlers: repo add/list/remove, status."""

from __future__ import annotations

from lcsas.cli.main import main


def _get_repo_id_by_name(db_path: str, name: str) -> str:
    """Helper: look up the UUID for a repo registered by ``name``."""
    from lcsas.db.connection import get_connection
    from lcsas.db.repos import list_repos

    conn = get_connection(db_path)
    try:
        for repo in list_repos(conn):
            if repo.name == name:
                return repo.repo_id
    finally:
        conn.close()
    raise AssertionError(f"repo '{name}' not registered")


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


class TestCmdRepoRemove:
    def test_repo_remove_happy_path(self, tmp_path, capsys):
        """repo remove deletes the row and logs a success message."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed repository 'family'" in out

        # Row is gone from the catalog.
        from lcsas.db.connection import get_connection
        from lcsas.db.repos import list_repos

        conn = get_connection(str(db))
        try:
            assert [r.name for r in list_repos(conn)] == []
        finally:
            conn.close()

    def test_repo_remove_unknown_repo_errors(self, tmp_path, capsys):
        """repo remove with an unknown id exits non-zero with an error."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", "no-such-name"])
        assert result != 0
        out = capsys.readouterr().out
        assert "no-such-name" in out
        assert "not found" in out

    def test_repo_remove_rejects_active_packs_without_force(
        self, tmp_path, capsys
    ):
        """repo remove refuses while active packs sit on active volumes."""
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import register_pack
        from lcsas.db.volume_packs import link_pack_to_volume
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")

        # Add an active pack on a non-deprecated volume.
        conn = get_connection(str(db))
        try:
            vol = create_volume(
                conn,
                label="V1",
                uuid=generate_uuid(),
                media_type="BD25",
                capacity_bytes=25_000_000_000,
                status="BURNED",
            )
            pack = register_pack(
                conn, sha256="active_pack_1", size_bytes=4096, repo_id=repo_id
            )
            link_pack_to_volume(conn, vol.volume_id, pack.pack_id)
        finally:
            conn.close()
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result != 0
        out = capsys.readouterr().out
        assert "family" in out
        assert "active volumes" in out
        assert "--force" in out

        # Repo and pack are still in the catalog.
        conn = get_connection(str(db))
        try:
            from lcsas.db.packs import list_packs
            from lcsas.db.repos import list_repos

            assert "family" in [r.name for r in list_repos(conn)]
            assert len(list_packs(conn, repo_id=repo_id, include_pruned=True)) == 1
        finally:
            conn.close()

    def test_repo_remove_with_force_purges_packs(
        self, tmp_path, capsys, monkeypatch
    ):
        """repo remove --force marks packs pruned and deletes the repo."""
        from lcsas.db.connection import get_connection
        from lcsas.db.packs import list_packs, register_pack
        from lcsas.db.repos import list_repos
        from lcsas.db.volume_packs import link_pack_to_volume
        from lcsas.db.volumes import create_volume
        from lcsas.utils.labels import generate_uuid

        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "family", str(tmp_path / "mirror")])
        repo_id = _get_repo_id_by_name(str(db), "family")

        conn = get_connection(str(db))
        try:
            vol = create_volume(
                conn,
                label="V1",
                uuid=generate_uuid(),
                media_type="BD25",
                capacity_bytes=25_000_000_000,
                status="BURNED",
            )
            pack = register_pack(
                conn, sha256="force_pack_1", size_bytes=4096, repo_id=repo_id
            )
            link_pack_to_volume(conn, vol.volume_id, pack.pack_id)
        finally:
            conn.close()

        # --force prompts via input(); auto-confirm.
        monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")
        capsys.readouterr()  # clear

        result = main(["--db", str(db), "repo", "remove", repo_id, "--force"])
        assert result == 0
        out = capsys.readouterr().out
        assert "Removed repository 'family'" in out

        # Repo gone; packs gone; volume_packs links gone.
        conn = get_connection(str(db))
        try:
            assert [r.name for r in list_repos(conn)] == []
            assert list_packs(conn, repo_id=repo_id, include_pruned=True) == []
            row = conn.execute(
                "SELECT COUNT(*) FROM volume_packs WHERE pack_id = ?",
                (pack.pack_id,),
            ).fetchone()
            assert row[0] == 0
        finally:
            conn.close()


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
