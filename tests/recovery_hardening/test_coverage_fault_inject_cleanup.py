"""Hardening test: the coverage fault sweep leaves no test fixtures behind.

Catches: ``recovery/scripts/run_coverage_fault_inject.sh`` (coverage-c
step 3c, so audit-gate step 1) running every ``build/test_*`` binary
under malloc_inject with no cleanup.  A faulted run leaves through the
gcov shim's ``_exit()`` and a stuck one is killed by ``timeout``, so the
cleanup at the end of the test's ``main()`` never runs.  Every sweep
left hundreds of ``lcsas_dl_*`` fixture folders in /tmp and /dev/shm,
and once test_disc_locator gained its 24 MiB drain-on-first-sight case
(rc6), one sweep's leftovers in /dev/shm (RAM) filled a build host's
swap.

Two halves, both pinned here:

* ``test_disc_locator.c`` makes its fixtures under ``$TMPDIR`` (it used
  a fixed ``/tmp/lcsas_dl_test_XXXXXX``), so a killed run leaves them
  where its caller can remove them;
* the sweep gives every run its own ``TMPDIR``, removes it after the
  run, and removes our own ``/dev/shm/lcsas_dl_*_<pid>`` folders whose
  process is gone (a live one is kept).

Skips when ``recovery/build/test_disc_locator`` is not built (run
``make -C recovery all test``), or there is no C compiler for the
malloc_inject shim or no ``timeout``.
"""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

from .conftest import _scaled_timeout

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"
SWEEP = RECOVERY / "scripts" / "run_coverage_fault_inject.sh"
SHIM_SRC = RECOVERY / "scripts" / "malloc_inject.c"
TEST_BIN = RECOVERY / "build" / "test_disc_locator"
SHM = Path("/dev/shm")


def _need_test_bin() -> None:
    if not TEST_BIN.is_file():
        pytest.skip("recovery/build/test_disc_locator not built "
                    "(run `make -C recovery all test`)")


def _dead_pid() -> int:
    """The pid of a process that has just exited."""
    proc = subprocess.run(["sh", "-c", "echo $$"], capture_output=True,
                          text=True, check=True)
    return int(proc.stdout.strip())


def test_disc_locator_fixtures_live_under_tmpdir(tmp_path: Path) -> None:
    """A test_disc_locator run killed midway leaves its fixtures under
    $TMPDIR, not in a fixed /tmp folder its caller does not know."""
    _need_test_bin()
    tmpdir = tmp_path / "t"
    tmpdir.mkdir()
    proc = subprocess.Popen(
        [str(TEST_BIN)], env={**os.environ, "TMPDIR": str(tmpdir)},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    seen: list[str] = []
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            seen = [p.name for p in tmpdir.iterdir()
                    if p.name.startswith("lcsas_dl_test_")]
            if seen:
                break
            time.sleep(0.01)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGKILL)
        proc.wait(timeout=_scaled_timeout(30))
    assert seen, (
        "test_disc_locator made no lcsas_dl_test_* folder under the "
        f"TMPDIR it was given ({tmpdir}) while it ran; a killed run "
        "would leave its fixtures where no caller removes them."
    )


def test_fault_sweep_removes_what_its_runs_leave(tmp_path: Path) -> None:
    """A short sweep of test_disc_locator leaves nothing in the TMPDIR
    it was given, and it removes a dead-pid lcsas_dl_* folder of ours in
    /dev/shm but keeps a live one.  Its two faulted runs stop before the
    test's own cleanup (without the sweep's cleanup they leave a dozen
    fixture folders); the 1 s timeout only keeps the gate short."""
    _need_test_bin()
    cc = shutil.which("cc")
    if cc is None or shutil.which("timeout") is None:
        pytest.skip("needs cc (for the malloc_inject shim) and timeout")
    shim = tmp_path / "malloc_inject.so"
    subprocess.run(
        [cc, "-shared", "-fPIC", "-O0", "-D_GNU_SOURCE", str(SHIM_SRC),
         "-o", str(shim), "-ldl"],
        check=True, capture_output=True, timeout=120,
    )
    sweep_tmp = tmp_path / "sweep"
    sweep_tmp.mkdir()

    planted: list[Path] = []
    use_shm = SHM.is_dir() and os.access(SHM, os.W_OK)
    if use_shm:
        dead = SHM / f"lcsas_dl_gatetest_{_dead_pid()}"
        live = SHM / f"lcsas_dl_gatetest_{os.getpid()}"
        for d in (dead, live):
            (d / "data").mkdir(parents=True)
            planted.append(d)
    try:
        res = subprocess.run(
            ["sh", str(SWEEP), "2"],
            env={**os.environ, "TMPDIR": str(sweep_tmp),
                 "COV_FI_LIB": str(shim),
                 "COV_FI_TESTS": "test_disc_locator",
                 "PER_RUN_TIMEOUT": "1"},
            capture_output=True, text=True, timeout=600,
        )
        out = res.stdout + res.stderr
        assert res.returncode == 0, out
        assert "test_disc_locator: sweeping N=1..2" in out, (
            f"the sweep did not run test_disc_locator under the shim.\n{out}"
        )
        left = sorted(p.name for p in sweep_tmp.iterdir())
        assert not left, (
            f"the sweep left these in its TMPDIR: {left}\n{out}"
        )
        if use_shm:
            assert not planted[0].exists(), (
                f"a dead-pid {planted[0]} was not removed\n{out}"
            )
            assert planted[1].exists(), (
                f"a live-pid {planted[1]} was removed\n{out}"
            )
    finally:
        for d in planted:
            shutil.rmtree(d, ignore_errors=True)


def test_fault_sweep_cleans_up_when_stopped(tmp_path: Path) -> None:
    """A sweep stopped from outside (SIGTERM to its process group, as an
    outer ``timeout`` or make does) still removes its run folder and
    reaps dead-pid /dev/shm folders.  On 2026-11-11 one interrupted
    sweep left 1,162 drain-cache folders in /dev/shm (RAM)."""
    _need_test_bin()
    cc = shutil.which("cc")
    if cc is None or shutil.which("timeout") is None:
        pytest.skip("needs cc (for the malloc_inject shim) and timeout")
    shim = tmp_path / "malloc_inject.so"
    subprocess.run(
        [cc, "-shared", "-fPIC", "-O0", "-D_GNU_SOURCE", str(SHIM_SRC),
         "-o", str(shim), "-ldl"],
        check=True, capture_output=True, timeout=120,
    )
    sweep_tmp = tmp_path / "sweep"
    sweep_tmp.mkdir()
    use_shm = SHM.is_dir() and os.access(SHM, os.W_OK)
    dead = SHM / f"lcsas_dl_stoptest_{_dead_pid()}"
    if use_shm:
        (dead / "data").mkdir(parents=True)
    proc = subprocess.Popen(
        ["sh", str(SWEEP), "50"],
        env={**os.environ, "TMPDIR": str(sweep_tmp),
             "COV_FI_LIB": str(shim),
             "COV_FI_TESTS": "test_disc_locator",
             "PER_RUN_TIMEOUT": "60"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and proc.poll() is None:
            if any(p.name.startswith("lcsas-cov-fi.")
                   for p in sweep_tmp.iterdir()):
                break
            time.sleep(0.05)
        assert proc.poll() is None, "the sweep ended before it was stopped"
        os.killpg(proc.pid, signal.SIGTERM)
        rc = proc.wait(timeout=_scaled_timeout(60))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait(timeout=_scaled_timeout(30))
        if use_shm and dead.exists():
            shutil.rmtree(dead)
            pytest.fail(f"a dead-pid {dead} was not reaped after SIGTERM")
    assert rc == 143, f"expected exit 143 after SIGTERM, got {rc}"
    left = sorted(p.name for p in sweep_tmp.iterdir())
    assert not left, f"the stopped sweep left these in its TMPDIR: {left}"
