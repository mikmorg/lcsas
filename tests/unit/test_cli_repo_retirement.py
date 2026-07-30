"""CLI surface for repository retirement (#437).

`lcsas repo retire <id>` marks a repo whose Rustic mirror is gone for
good, so `lcsas meta build`'s key gate stops demanding keys LCSAS can no
longer reach.  It is deliberately NOT destructive — `repo remove --force`
deletes pack and snapshot history, and an operator whose packs are on
burned discs must never be pushed toward that verb just to silence a
gate.  Retirement is therefore a reversible flag with no confirmation
prompt, and `repo activate` is its inverse.

These tests drive main() only, so they also pin the argparse wiring and
the dispatch table, not just the CRUD underneath.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.repos import get_repo, list_repos


def _init_with_repo(tmp_path: Path, name: str = "family") -> tuple[Path, str]:
    """init + repo add; returns (db_path, repo_id)."""
    db = tmp_path / "archive.db"
    mirror = tmp_path / name
    mirror.mkdir(exist_ok=True)
    assert main(["init", "--db-path", str(db)]) == 0
    assert main(["--db", str(db), "repo", "add", name, str(mirror)]) == 0
    conn = get_connection(db)
    try:
        repo_id = next(r.repo_id for r in list_repos(conn) if r.name == name)
    finally:
        conn.close()
    return db, repo_id


def _status(db: Path, repo_id: str) -> str:
    conn = get_connection(db)
    try:
        return get_repo(conn, repo_id).status
    finally:
        conn.close()


class TestRepoRetireActivate:
    def test_new_repo_is_active(self, tmp_path):
        db, repo_id = _init_with_repo(tmp_path)
        assert _status(db, repo_id) == "active"

    def test_retire_then_activate(self, tmp_path):
        db, repo_id = _init_with_repo(tmp_path)

        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        assert _status(db, repo_id) == "retired"

        assert main(["--db", str(db), "repo", "activate", repo_id]) == 0
        assert _status(db, repo_id) == "active"

    def test_retire_needs_no_confirmation(self, tmp_path, monkeypatch):
        """Unlike `repo remove --force`, retirement never prompts — it
        destroys nothing.  Any read from stdin here is a bug."""
        db, repo_id = _init_with_repo(tmp_path)

        def _boom(*a, **kw):  # pragma: no cover - only runs on regression
            raise AssertionError("repo retire must not prompt for input")

        monkeypatch.setattr("builtins.input", _boom)
        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        assert _status(db, repo_id) == "retired"

    def test_retire_is_idempotent(self, tmp_path):
        db, repo_id = _init_with_repo(tmp_path)
        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        assert _status(db, repo_id) == "retired"

    def test_unknown_repo_id_errors(self, tmp_path, caplog):
        db, _ = _init_with_repo(tmp_path)
        caplog.clear()
        assert main(["--db", str(db), "repo", "retire", "no-such-repo"]) == 1
        assert "not found" in caplog.text

    def test_activate_unknown_repo_id_errors(self, tmp_path, caplog):
        db, _ = _init_with_repo(tmp_path)
        caplog.clear()
        assert main(["--db", str(db), "repo", "activate", "no-such-repo"]) == 1
        assert "not found" in caplog.text

    def test_retirement_keeps_the_repo_listed(self, tmp_path, capsys):
        """A retired repo stays in the catalog — its packs are on discs."""
        db, repo_id = _init_with_repo(tmp_path)
        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        capsys.readouterr()
        assert main(["--db", str(db), "repo", "list"]) == 0
        out = capsys.readouterr().out
        assert repo_id in out

    def test_repo_list_renders_status_column(self, tmp_path, capsys):
        db, repo_id = _init_with_repo(tmp_path)
        capsys.readouterr()
        assert main(["--db", str(db), "repo", "list"]) == 0
        assert "active" in capsys.readouterr().out

        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0
        capsys.readouterr()
        assert main(["--db", str(db), "repo", "list"]) == 0
        assert "retired" in capsys.readouterr().out

    def test_retire_migrates_a_v9_catalog_in_place(self, tmp_path):
        """The catalogs most likely to need retiring are old ones.  A v9
        catalog has no `status` column, so `repo retire` must migrate
        before it updates rather than fail on the UPDATE.
        """
        import sqlite3

        from lcsas.db.schema import CURRENT_SCHEMA_VERSION, get_schema_version

        db, repo_id = _init_with_repo(tmp_path)

        # Rewind the catalog to a genuine v9 shape: drop the column by
        # rebuilding the table, and roll the recorded version back.
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            ALTER TABLE repositories RENAME TO repositories_v9_tmp;
            CREATE TABLE repositories (
                repo_id          TEXT PRIMARY KEY,
                name             TEXT NOT NULL,
                mirror_path      TEXT NOT NULL,
                encryption_key_id TEXT NOT NULL DEFAULT '',
                created_at       DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO repositories
                (repo_id, name, mirror_path, encryption_key_id, created_at)
            SELECT repo_id, name, mirror_path, encryption_key_id, created_at
            FROM repositories_v9_tmp;
            DROP TABLE repositories_v9_tmp;
            DELETE FROM schema_version WHERE version >= 10;
            INSERT INTO schema_version (version) VALUES (9);
            """
        )
        conn.commit()
        assert get_schema_version(conn) == 9
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info(repositories)")
        }
        assert "status" not in cols
        conn.close()

        assert main(["--db", str(db), "repo", "retire", repo_id]) == 0

        conn = sqlite3.connect(db)
        try:
            assert get_schema_version(conn) == CURRENT_SCHEMA_VERSION
        finally:
            conn.close()
        assert _status(db, repo_id) == "retired"
