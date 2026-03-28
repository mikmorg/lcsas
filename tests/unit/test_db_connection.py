"""Tests for the SQLite connection and locking layer."""

from __future__ import annotations

import sys
import threading

import pytest

from lcsas.db.connection import get_connection, locked_connection
from lcsas.db.repos import list_repos, register_repo
from lcsas.db.schema import create_all
from lcsas.utils.labels import generate_uuid


class TestGetConnection:
    def test_creates_db_file(self, tmp_path):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        conn.close()
        assert db.exists()

    def test_db_file_permissions(self, tmp_path):
        """DB file must be owner-readable only (mode 0o600)."""
        db = tmp_path / "secure.db"
        conn = get_connection(db)
        conn.close()
        mode = db.stat().st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"

    def test_wal_mode_enabled(self, tmp_path):
        db = tmp_path / "wal.db"
        conn = get_connection(db)
        row = conn.execute("PRAGMA journal_mode;").fetchone()
        conn.close()
        assert row[0] == "wal"

    def test_foreign_keys_enabled(self, tmp_path):
        db = tmp_path / "fk.db"
        conn = get_connection(db)
        row = conn.execute("PRAGMA foreign_keys;").fetchone()
        conn.close()
        assert row[0] == 1

    def test_row_factory_set(self, tmp_path):
        import sqlite3
        db = tmp_path / "row.db"
        conn = get_connection(db)
        assert conn.row_factory is sqlite3.Row
        conn.close()

    def test_idempotent_open(self, tmp_path):
        """Opening the same DB twice is safe and returns different connection objects."""
        db = tmp_path / "multi.db"
        c1 = get_connection(db)
        c2 = get_connection(db)
        assert c1 is not c2
        c1.close()
        c2.close()


class TestLockedConnection:
    def test_basic_write_and_read(self, tmp_path):
        db = tmp_path / "locked.db"
        with locked_connection(db) as conn:
            create_all(conn)
            register_repo(conn, generate_uuid(), "repo1", "/mnt/r1", "")

        with locked_connection(db) as conn:
            repos = list_repos(conn)
        assert len(repos) == 1
        assert repos[0].name == "repo1"

    def test_exception_releases_lock(self, tmp_path):
        """Lock is released even when the body raises."""
        db = tmp_path / "exc.db"
        with pytest.raises(RuntimeError), locked_connection(db) as conn:
            create_all(conn)
            raise RuntimeError("intentional")

        # Lock should be free; this must not deadlock
        with locked_connection(db) as conn:
            assert list_repos(conn) == []

    @pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
    def test_concurrent_writes_no_data_loss(self, tmp_path):
        """N threads writing via locked_connection all succeed without data loss."""
        db = tmp_path / "concurrent.db"
        # Pre-create schema once
        with locked_connection(db) as conn:
            create_all(conn)

        n_threads = 8
        errors: list[Exception] = []

        def _write_one(idx: int) -> None:
            repo_id = generate_uuid()
            try:
                with locked_connection(db) as conn:
                    register_repo(
                        conn,
                        repo_id=repo_id,
                        name=f"repo_{idx}",
                        mirror_path=f"/mnt/mirror_{idx}",
                        encryption_key_id="",
                    )
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=_write_one, args=(i,)) for i in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors, f"Thread errors: {errors}"

        with locked_connection(db) as conn:
            repos = list_repos(conn)
        assert len(repos) == n_threads, (
            f"Expected {n_threads} repos, got {len(repos)}"
        )

    @pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
    def test_shared_lock_allows_concurrent_reads(self, tmp_path):
        """Multiple threads can hold shared locks simultaneously."""
        db = tmp_path / "shared.db"
        with locked_connection(db) as conn:
            create_all(conn)

        barrier = threading.Barrier(4)
        results: list[list] = []

        def _read() -> None:
            with locked_connection(db, exclusive=False) as conn:
                barrier.wait()  # all threads inside the shared lock at once
                results.append(list_repos(conn))

        threads = [threading.Thread(target=_read) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(results) == 4
        assert all(r == [] for r in results)
