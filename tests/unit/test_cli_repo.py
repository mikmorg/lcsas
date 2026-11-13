"""Tests for the ``lcsas repo`` command (``lcsas.cli.commands.repo``)."""

from __future__ import annotations

from lcsas.cli.main import main
from lcsas.db.connection import get_connection


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


class TestCmdRepoAddEdges:
    def test_duplicate_repo_name_errors(self, tmp_path, capsys):
        """A second repository with the same name is refused (#685): it
        would make every later ``--repo dup`` ambiguous."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        r1 = main(["--db", str(db), "repo", "add", "dup", "/p1"])
        r2 = main(["--db", str(db), "repo", "add", "dup", "/p2"])
        assert r1 == 0
        assert r2 == 1


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


class TestCmdRepoListEdges:
    def test_many_repos(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        for i in range(10):
            main(["--db", str(db), "repo", "add", f"repo{i}", f"/mnt/{i}"])
        capsys.readouterr()

        result = main(["--db", str(db), "repo", "list"])
        assert result == 0
        out = capsys.readouterr().out
        for i in range(10):
            assert f"repo{i}" in out


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
        from lcsas.db.volume_packs import bulk_link_packs
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
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
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
        from lcsas.db.volume_packs import bulk_link_packs
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
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
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


class TestCmdRepoRemoveEdges:
    def test_remove_nonexistent_repo(self, tmp_path, capsys):
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        result = main(["--db", str(db), "repo", "remove", "nonexistent"])
        assert result == 1
        out = capsys.readouterr().out
        assert "not found" in out.lower()

    def test_remove_with_packs_needs_force(self, tmp_path, capsys):
        """Removing repo with active packs without --force fails."""
        db = tmp_path / "test.db"
        main(["init", "--db-path", str(db)])
        main(["--db", str(db), "repo", "add", "packed", "/mnt/packed"])

        conn = get_connection(db)
        from lcsas.db.repos import list_repos
        repos = [r for r in list_repos(conn) if r.name == "packed"]
        repo_id = repos[0].repo_id
        from lcsas.db.packs import register_pack
        register_pack(conn, "cc" * 32, 1000, repo_id)
        conn.close()

        capsys.readouterr()
        result = main(["--db", str(db), "repo", "remove", repo_id])
        assert result == 1
        out = capsys.readouterr().out
        assert "force" in out.lower()
