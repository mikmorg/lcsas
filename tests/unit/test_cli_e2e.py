"""End-to-end CLI pipeline tests.

These tests exercise the full command sequence using *only* main() — no
direct DB calls for setup — verifying that state flows correctly between
commands.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.repos import list_repos


def _write_config(tmp_path: Path, mirror: Path, db: Path) -> Path:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(textwrap.dedent(f"""\
        [paths]
        mirror_base = "{mirror}"
        staging = "{staging}"
        database = "{db}"

        [defaults]
        media_type = "TEST_TINY"
        metadata_reserve_mb = 0

        [repos.test_repo]
        mirror_path = "{mirror}"
    """))
    return cfg


class TestInitRepoScanPipeline:
    """Happy-path: init → repo add → scan → status."""

    def test_pipeline_init_to_scan(self, tmp_path, capsys):
        """Full pipeline produces correct DB state, observable via CLI commands."""
        db = tmp_path / "archive.db"
        mirror = tmp_path / "mirror"
        mirror.mkdir()

        # 1. init creates the database
        assert main(["init", "--db-path", str(db)]) == 0
        assert db.exists()

        # 2. repo add registers the repository
        assert main(["--db", str(db), "repo", "add", "test_repo", str(mirror)]) == 0

        # 3. repo list shows the new repo
        capsys.readouterr()
        assert main(["--db", str(db), "repo", "list"]) == 0
        assert "test_repo" in capsys.readouterr().out

        # 4. Place a fake pack file in the mirror (two-level layout)
        data_dir = mirror / "data" / "ab"
        data_dir.mkdir(parents=True)
        fake_sha = "ab" + "cd" * 31  # 64 hex chars
        (data_dir / fake_sha).write_bytes(b"\x00" * 2048)

        # 5. Write a config TOML and scan (no snapshot listing needed)
        cfg = _write_config(tmp_path, mirror, db)
        capsys.readouterr()
        assert main(["--config", str(cfg), "--db", str(db), "scan", "--no-snapshots"]) == 0
        scan_out = capsys.readouterr().out
        assert "Total scanned: 1" in scan_out
        assert "New packs registered: 1" in scan_out

        # 6. status reflects the registered pack
        capsys.readouterr()
        assert main(["--db", str(db), "status"]) == 0
        status_out = capsys.readouterr().out
        assert "Packs: 1 total" in status_out
        assert "1 unarchived" in status_out
        assert "0 archived" in status_out

    def test_second_scan_discovers_no_new_packs(self, tmp_path, capsys):
        """Re-scanning the same mirror does not re-register already-known packs."""
        db = tmp_path / "archive.db"
        mirror = tmp_path / "mirror"
        mirror.mkdir()

        assert main(["init", "--db-path", str(db)]) == 0
        assert main(["--db", str(db), "repo", "add", "test_repo", str(mirror)]) == 0

        data_dir = mirror / "data" / "cc"
        data_dir.mkdir(parents=True)
        fake_sha = "cc" + "ee" * 31
        (data_dir / fake_sha).write_bytes(b"\x01" * 512)

        cfg = _write_config(tmp_path, mirror, db)

        # First scan registers the pack
        capsys.readouterr()
        assert main(["--config", str(cfg), "--db", str(db), "scan", "--no-snapshots"]) == 0
        first_out = capsys.readouterr().out
        assert "New packs registered: 1" in first_out

        # Second scan finds no new packs
        capsys.readouterr()
        assert main(["--config", str(cfg), "--db", str(db), "scan", "--no-snapshots"]) == 0
        second_out = capsys.readouterr().out
        assert "New packs registered: 0" in second_out

    def test_status_empty_db(self, tmp_path, capsys):
        """Status on a freshly initialised DB reports zero packs and volumes."""
        db = tmp_path / "empty.db"
        assert main(["init", "--db-path", str(db)]) == 0

        capsys.readouterr()
        assert main(["--db", str(db), "status"]) == 0
        out = capsys.readouterr().out
        assert "Packs: 0 total" in out
        assert "Volumes: 0 total" in out


class TestRepoRemovePipeline:
    """Repo removal via CLI commands only."""

    def test_remove_empty_repo_via_cli(self, tmp_path, capsys):
        """An empty repo (no packs) can be removed via CLI using its ID."""
        db = tmp_path / "archive.db"
        mirror = tmp_path / "mirror"
        mirror.mkdir()

        assert main(["init", "--db-path", str(db)]) == 0
        assert main(["--db", str(db), "repo", "add", "temp_repo", str(mirror)]) == 0

        # Extract repo_id directly from the catalog
        conn = get_connection(db)
        try:
            repos = list_repos(conn)
        finally:
            conn.close()
        assert len(repos) == 1
        repo_id = repos[0].repo_id

        # Remove the repo
        assert main(["--db", str(db), "repo", "remove", repo_id]) == 0

        # repo list should now be empty
        conn = get_connection(db)
        try:
            assert list_repos(conn) == []
        finally:
            conn.close()
