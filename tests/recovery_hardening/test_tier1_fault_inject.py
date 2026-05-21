"""Audit phase 6 (issue #165): malloc fault-injection sweep — zero crashes.

The `make fault-inject` target uses an LD_PRELOAD shim to fail the Nth
allocation across a sweep of N=1..total, for every test binary under
`recovery/build/test_*`.  A graceful error return is fine; a crash
(SIGSEGV / SIGABRT) means a malloc failure was not handled and an
attacker (or natural OOM) could trigger a crash.

This test is opt-in via ``LCSAS_FAULT_INJECT=1`` because the sweep
takes ~40 seconds per heavy test binary (test_catalog: 820 N).
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("LCSAS_FAULT_INJECT"),
    reason="set LCSAS_FAULT_INJECT=1 to run the malloc fault-injection sweep",
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY = REPO_ROOT / "recovery"


def test_fault_inject_sweep_no_crashes() -> None:
    """`make fault-inject` must exit 0 with no crash signals.

    Each binary is run with LCSAS_FAIL_AT=N for every N from 1 to
    min(total, MAX_N).  Exit codes in {SIGSEGV, SIGABRT, SIGKILL,
    SIGBUS, SIGFPE} or a timeout are treated as production-code bugs
    in error-handling paths.
    """
    if shutil.which("cc") is None:
        pytest.skip("cc not installed")
    # Use a smaller MAX_N for CI/dev runs so the test stays under 1 min.
    max_n = os.environ.get("LCSAS_FAULT_INJECT_MAX_N", "100")
    res = subprocess.run(
        ["make", "-C", str(RECOVERY), "fault-inject", f"MAX_N={max_n}"],
        capture_output=True, text=True, timeout=900,
    )
    assert res.returncode == 0, (
        f"fault-inject reported a crash:\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "0 crashes across all test binaries" in res.stdout, (
        f"unexpected fault-inject output:\nstdout:\n{res.stdout}"
    )
