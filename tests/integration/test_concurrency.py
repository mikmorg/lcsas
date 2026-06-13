"""FUP-02 integration: real two-process catalog-lock behaviour.

No external binaries required (always-on).  Spawns real ``lcsas`` processes
that contend for the catalog flock and asserts the FUP-02 mitigations:

* a read-only command (``status``) succeeds while another process holds the
  lock — it never takes the flock;
* a writer command (``scan``) blocked behind the held lock prints the
  loud, holder-identifying "waiting for the catalog lock" message instead of
  hanging silently;
* a process can fail fast with exit code 75 via ``--lock-timeout`` rather
  than waiting forever.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import pytest

from lcsas.db.connection import locked_connection
from lcsas.db.schema import create_all

pytestmark = pytest.mark.integration


def _write_config(tmp_path: Path) -> tuple[Path, Path]:
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    mirror = tmp_path / "mirror"
    mirror.mkdir(exist_ok=True)
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
"""
    )
    return cfg, db


def _init_catalog(db: Path) -> None:
    with locked_connection(db) as conn:
        create_all(conn)


def _lcsas(args: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "lcsas.cli.main", *args],
        capture_output=True, text=True, check=False, **kw,
    )


def _hold_lock(db: Path, hold_seconds: float) -> subprocess.Popen:
    """Spawn a child holding the catalog lock; return once it confirms."""
    code = (
        "import time\n"
        "from lcsas.db.connection import set_lock_holder_label, locked_connection\n"
        "set_lock_holder_label('lcsas burn session')\n"
        f"with locked_connection({str(db)!r}) as conn:\n"
        "    print('HELD', flush=True)\n"
        f"    time.sleep({hold_seconds})\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "HELD"
    return proc


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_readonly_status_succeeds_under_held_lock(tmp_path):
    """`lcsas status` is genuinely read-only — it ignores a held writer lock."""
    cfg, db = _write_config(tmp_path)
    _init_catalog(db)
    proc = _hold_lock(db, hold_seconds=3.0)
    try:
        start = time.monotonic()
        res = _lcsas(["--config", str(cfg), "status"])
        elapsed = time.monotonic() - start
    finally:
        proc.wait(timeout=10)
    assert res.returncode == 0, res.stderr
    # Did not block on the lock (held for 3s; status must return well under it).
    assert elapsed < 2.5, f"status appears to have waited on the lock ({elapsed:.1f}s)"


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_writer_prints_waiting_message_then_proceeds(tmp_path):
    """A blocked writer prints the holder-identifying wait message."""
    cfg, db = _write_config(tmp_path)
    _init_catalog(db)
    proc = _hold_lock(db, hold_seconds=1.5)
    try:
        res = _lcsas(["--config", str(cfg), "scan"])
    finally:
        proc.wait(timeout=10)
    assert "Waiting for the catalog lock" in res.stderr
    assert "lcsas burn session" in res.stderr
    assert str(proc.pid) in res.stderr
    assert "Do NOT kill" in res.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_lock_timeout_exits_75(tmp_path):
    """--lock-timeout makes a blocked writer exit 75 instead of hanging."""
    cfg, db = _write_config(tmp_path)
    _init_catalog(db)
    proc = _hold_lock(db, hold_seconds=5.0)
    try:
        res = _lcsas(["--lock-timeout", "0.3", "--config", str(cfg), "scan"])
    finally:
        proc.wait(timeout=10)
    assert res.returncode == 75, (res.returncode, res.stderr)
    assert "lcsas burn session" in res.stderr


@pytest.mark.skipif(sys.platform == "win32", reason="fcntl not available on Windows")
def test_staging_clean_does_not_delete_in_flight_session(tmp_path):
    """Charter (a) reproduction: staging-clean must spare a dir whose session
    is committed during the confirm prompt.

    A barrier process commits a burn_sessions row for one flagged dir while
    `staging clean` (without --force) blocks on stdin; the re-check under the
    held lock must remove only the still-orphaned dir.
    """
    cfg, db = _write_config(tmp_path)
    _init_catalog(db)
    staging = tmp_path / "staging"
    keep = staging / "2025-01-01T00-00-00.000000+00-00-aaaaaaaa"
    gone = staging / "2025-01-02T00-00-00.000000+00-00-bbbbbbbb"
    keep.mkdir(parents=True)
    gone.mkdir(parents=True)

    # Start staging-clean interactively; it will hold the lock and block on
    # the confirm prompt until we feed it "y".
    proc = subprocess.Popen(
        [sys.executable, "-m", "lcsas.cli.main",
         "--config", str(cfg), "staging", "clean"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True,
    )
    try:
        # Give it a moment to detect + reach the prompt while holding the lock.
        time.sleep(1.0)
        # Commit a session row for `keep` from a second connection (writes are
        # allowed under WAL even while the flock is held by staging-clean).
        from lcsas.db.connection import get_connection
        from lcsas.db.sessions import create_session
        conn = get_connection(db)
        create_session(
            conn, media_type="TEST_TINY", staging_dir=str(keep),
            session_id="2025-01-01T00:00:00.000000+00:00-aaaaaaaa",
        )
        conn.close()
        out, err = proc.communicate(input="y\n", timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, err
    assert keep.exists(), "in-flight session dir was wrongly deleted"
    assert not gone.exists(), "stale orphan was not removed"


def test_python_module_entrypoint_runs():
    """Sanity: `python -m lcsas.cli.main` is the invocation the tests rely on."""
    res = _lcsas(["--version"])
    assert res.returncode == 0
    assert "lcsas" in (res.stdout + res.stderr).lower()
