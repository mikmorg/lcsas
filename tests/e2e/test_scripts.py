"""Pytest wrappers for the standalone end-to-end scripts.

These tests invoke ``scripts/e2e_test.py`` and ``scripts/smoke_single_drive.py``
as subprocesses so they participate in ``make test-all``. The scripts
themselves drive real ``rustic``, ``xorriso``, and (for the smoke test)
``cdemu`` against a hardcoded ``/mnt/lcsas-data`` LV — no mocks.

Each script is skipped cleanly when its required external tooling is
absent, matching the behaviour of the rest of ``tests/integration/``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPO_ROOT / "scripts" / "e2e_test.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_single_drive.py"


def _format_failure(result: subprocess.CompletedProcess[bytes], script: Path) -> str:
    """Build a helpful assertion message including captured stdout/stderr."""
    stdout = result.stdout.decode(errors="replace") if result.stdout else ""
    stderr = result.stderr.decode(errors="replace") if result.stderr else ""
    return (
        f"{script.name} exited with rc={result.returncode}\n"
        f"--- stdout ---\n{stdout}\n"
        f"--- stderr ---\n{stderr}"
    )


@pytest.mark.requires_rustic
@pytest.mark.requires_xorriso
def test_e2e_pipeline(tmp_path: Path) -> None:
    """Run scripts/e2e_test.py as the canonical end-to-end pipeline test.

    The script uses a fixed ``/mnt/lcsas-data`` base (see ``setup_test_lv.sh``)
    and does not accept a ``--base`` argument, so ``tmp_path`` is only used
    by pytest for its own bookkeeping. The script must exit 0.
    """
    assert E2E_SCRIPT.is_file(), f"missing {E2E_SCRIPT}"
    result = subprocess.run(
        [sys.executable, str(E2E_SCRIPT)],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, _format_failure(result, E2E_SCRIPT)


@pytest.mark.requires_rustic
@pytest.mark.requires_xorriso
@pytest.mark.requires_cdemu
@pytest.mark.skipif(
    os.geteuid() != 0,
    reason="cdemu disc-swap loop requires root",
)
def test_smoke_single_drive(tmp_path: Path) -> None:
    """Run scripts/smoke_single_drive.py to exercise the single-drive restore.

    The script drives a cdemu virtual drive to simulate disc swaps, so it
    needs both the cdemu binary and root privileges (for the underlying
    ``sudo rm -rf`` cleanup and cdemu daemon control).
    """
    assert SMOKE_SCRIPT.is_file(), f"missing {SMOKE_SCRIPT}"
    result = subprocess.run(
        [sys.executable, str(SMOKE_SCRIPT)],
        check=False,
        capture_output=True,
    )
    assert result.returncode == 0, _format_failure(result, SMOKE_SCRIPT)
