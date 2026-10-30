"""The blind harness's run lock works across users and leaves no file (#559).

``run.sh`` runs as the invoking user (``make blind-restore``) and as root
(``run_variant.sh`` runs it under ``sudo -E``).  It used to lock
``/tmp/lcsas-blind-restore.lock`` through ``exec 9>``.  With
``fs.protected_regular=2`` (the Ubuntu default) an ``O_CREAT`` open of a
file in sticky, world-writable ``/tmp`` fails when another user owns it,
root included, so a lock file left by one entry point blocked the other
for good, after fixture setup ("Permission denied" at run.sh:14).

The lock is now a read-only fd on the harness directory.  These tests run
the lock step taken verbatim from run.sh: it blocks a second run while held,
passes once released, creates no file, and (when passwordless sudo is
available) blocks root while a plain user holds it.
"""

from __future__ import annotations

import fcntl
import os
import re
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
RUN_SH = REPO / "tests" / "e2e" / "cdemu_blind_restore" / "run.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("flock") is None, reason="flock(1) not installed",
)


def _lock_block() -> str:
    """The lock step of run.sh, from its comment through the closing ``fi``."""
    text = RUN_SH.read_text(encoding="utf-8")
    m = re.search(
        r"^# Prevent overlapping runs.*?^fi\n", text, re.MULTILINE | re.DOTALL,
    )
    assert m, "run.sh lost its '# Prevent overlapping runs' lock block"
    return m.group(0)


def _script(tmp_path: Path, here: Path) -> Path:
    script = tmp_path / "lock_step.sh"
    script.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        f'HERE="{here}"\n' + _lock_block() + 'echo "LOCKED"\n',
        encoding="utf-8",
    )
    return script


@contextmanager
def _held(directory: Path) -> Iterator[None]:
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(fd)


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def test_lock_is_a_read_only_directory_fd_not_a_tmp_file() -> None:
    block = _lock_block()
    assert 'exec 9<"$HERE"' in block
    assert "flock -n 9" in block
    # No write-mode open, no lock file under /tmp, nothing written to fd 9.
    assert not re.search(r"exec\s+9>", block)
    assert "/tmp/" not in block
    assert ">&9" not in RUN_SH.read_text(encoding="utf-8")


def test_lock_step_passes_when_free_and_creates_no_file(tmp_path: Path) -> None:
    here = tmp_path / "harness"
    here.mkdir()
    before = set(here.iterdir())
    r = _run(["bash", str(_script(tmp_path, here))])
    assert r.returncode == 0, r.stderr
    assert "LOCKED" in r.stdout
    assert set(here.iterdir()) == before


def test_lock_step_refuses_a_second_run(tmp_path: Path) -> None:
    here = tmp_path / "harness"
    here.mkdir()
    script = _script(tmp_path, here)
    with _held(here):
        r = _run(["bash", str(script)])
    assert r.returncode == 1
    assert "another blind-restore run is already in progress" in r.stderr
    assert "LOCKED" not in r.stdout
    # Released: the next run gets the lock.
    assert _run(["bash", str(script)]).returncode == 0


def _sudo_works() -> bool:
    if os.geteuid() == 0 or shutil.which("sudo") is None:
        return False
    return _run(["sudo", "-n", "true"]).returncode == 0


@pytest.mark.skipif(not _sudo_works(), reason="needs a non-root user with passwordless sudo")
def test_lock_works_across_user_and_root(tmp_path: Path) -> None:
    """The #559 case: one entry point runs as the user, the other as root."""
    here = tmp_path / "harness"
    here.mkdir()
    script = _script(tmp_path, here)
    with _held(here):
        r = _run(["sudo", "-n", "bash", str(script)])
    assert r.returncode == 1, r.stdout + r.stderr
    assert "another blind-restore run is already in progress" in r.stderr
    # Root ran the step after the user; then the user runs it again.
    assert _run(["sudo", "-n", "bash", str(script)]).returncode == 0
    assert _run(["bash", str(script)]).returncode == 0
    assert list(here.iterdir()) == []
