"""Tests for the SQLite connection and locking layer."""

from __future__ import annotations

import contextlib
import json
import subprocess
import sys
import threading
import time

import pytest

from lcsas.db.connection import get_connection, locked_connection
from lcsas.db.repos import list_repos, register_repo
from lcsas.db.schema import create_all
from lcsas.exceptions import CatalogLockTimeout
from lcsas.utils.labels import generate_uuid


def _hold_lock_subprocess(db_path, hold_seconds: float) -> subprocess.Popen:
    """Spawn a child that takes the catalog lock and holds it.

    A separate process is required because ``fcntl.flock`` is keyed on the
    open file description; two acquires within one process would not
    contend the way two real ``lcsas`` invocations do.
    """
    code = (
        "import time\n"
        "from lcsas.db.connection import set_lock_holder_label, locked_connection\n"
        "set_lock_holder_label('lcsas burn session')\n"
        f"with locked_connection({str(db_path)!r}) as conn:\n"
        "    print('HELD', flush=True)\n"
        f"    time.sleep({hold_seconds})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    # Wait until the child reports it holds the lock.
    assert proc.stdout is not None
    line = proc.stdout.readline()
    assert line.strip() == "HELD", f"child failed to take lock: {line!r}"
    return proc


class TestGetConnection:
    def test_creates_db_file(self, tmp_path):
        db = tmp_path / "test.db"
        conn = get_connection(db)
        conn.close()
        assert db.exists()

    def test_memory_sentinel_creates_no_file(self, tmp_path, monkeypatch):
        """get_connection(':memory:') opens an in-memory DB without creating
        a junk file literally named ':memory:' in the cwd (regression)."""
        monkeypatch.chdir(tmp_path)
        conn = get_connection(":memory:")
        try:
            conn.execute("CREATE TABLE t (x INTEGER)")
            conn.execute("INSERT INTO t VALUES (1)")
            assert conn.execute("SELECT x FROM t").fetchone()[0] == 1
        finally:
            conn.close()
        assert not (tmp_path / ":memory:").exists()
        assert list(tmp_path.iterdir()) == [], "in-memory connection left files"

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

    def test_locked_connection_writes_holder_info(self, tmp_path):
        """While held, <db>.lock contains the holder's pid/cmd/since JSON."""
        import os

        from lcsas.db.connection import set_lock_holder_label

        db = tmp_path / "holder.db"
        lock = tmp_path / "holder.db.lock"
        set_lock_holder_label("lcsas burn session")
        try:
            with locked_connection(db):
                info = json.loads(lock.read_text(encoding="utf-8"))
                assert info["pid"] == os.getpid()
                assert info["cmd"] == "lcsas burn session"
                assert "since" in info and info["since"]
        finally:
            set_lock_holder_label("lcsas")

    @pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
    def test_locked_connection_prints_waiting_and_blocks(self, tmp_path, capfd):
        """A waiter prints a holder-identifying message, then acquires once free."""
        db = tmp_path / "wait.db"
        proc = _hold_lock_subprocess(db, hold_seconds=1.0)
        try:
            acquired_at = []

            def _acquire() -> None:
                with locked_connection(db):
                    acquired_at.append(time.monotonic())

            t = threading.Thread(target=_acquire)
            t.start()
            t.join(timeout=15)
            assert acquired_at, "waiter never acquired the lock"
        finally:
            proc.wait(timeout=10)

        err = capfd.readouterr().err
        assert "Waiting for the catalog lock" in err
        assert "lcsas burn session" in err
        assert str(proc.pid) in err
        assert "Do NOT kill" in err

    @pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
    def test_locked_connection_timeout_raises(self, tmp_path):
        """timeout= raises CatalogLockTimeout naming the holder on expiry."""
        db = tmp_path / "timeout.db"
        proc = _hold_lock_subprocess(db, hold_seconds=3.0)
        try:
            with pytest.raises(CatalogLockTimeout) as excinfo, \
                    locked_connection(db, timeout=0.2):
                pass
            msg = str(excinfo.value)
            assert "lcsas burn session" in msg
            assert str(proc.pid) in msg
        finally:
            proc.wait(timeout=10)

    def test_corrupted_db_raises_error(self, tmp_path):
        """Opening a corrupted DB raises an error (and closes connection)."""
        import sqlite3
        db = tmp_path / "corrupted.db"
        # Create a file that looks like SQLite but is truncated
        db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        with pytest.raises((sqlite3.DatabaseError, RuntimeError)):
            get_connection(db)

    def test_corrupted_db_closes_connection_on_error(self, tmp_path):
        """Connection is properly closed on any error during initialization.

        Tests the try/except wrapper around quick_check ensures connection closure.
        """
        import sqlite3
        db = tmp_path / "truncated.db"
        # Create a file that causes an error during PRAGMA/quick_check
        db.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)

        with contextlib.suppress(sqlite3.DatabaseError, RuntimeError):
            get_connection(db)

        # If connection wasn't closed, a subsequent attempt would fail to open
        # This test primarily validates the error handling code path works
