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
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E_SCRIPT = REPO_ROOT / "scripts" / "e2e_test.py"
SMOKE_SCRIPT = REPO_ROOT / "scripts" / "smoke_single_drive.py"

# TEST_TINY media (1 MB) keeps the footprint tiny; require a small margin so we
# never wedge a near-full disk. The pipeline cleans up after itself.
_MIN_FREE_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB


def _force_rmtree(path: Path) -> None:
    """rmtree that survives the read-only tree xorriso extracts from ISOs.

    Packs/metadata pulled back out of an ISO9660 image come out read-only,
    *including the directories* (mode 0555) — so unlinking a file inside one
    is denied and a plain rmtree leaves the scratch base behind. Make every
    directory writable+executable first, then remove.
    """
    for root, dirs, _files in os.walk(path):
        os.chmod(root, stat.S_IRWXU)
        for d in dirs:
            os.chmod(os.path.join(root, d), stat.S_IRWXU)
    shutil.rmtree(path)


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
def test_e2e_pipeline() -> None:
    """Run scripts/e2e_test.py as the canonical end-to-end pipeline test.

    Portable across machines: the script reads its base directory from
    ``LCSAS_E2E_BASE`` (default ``/mnt/lcsas-data``). On a host where the
    default LV is absent (CI, a fresh checkout) we fall back to a private
    scratch base under ``/var/tmp`` and pass it through. We skip only when
    there isn't ~2 GiB free at the chosen base — never on path absence, so
    the gate can't pass green-by-skip where the tooling actually exists.
    """
    assert E2E_SCRIPT.is_file(), f"missing {E2E_SCRIPT}"

    env = os.environ.copy()
    configured_base = Path(env.get("LCSAS_E2E_BASE", "/mnt/lcsas-data"))

    cleanup_base: Path | None = None
    if configured_base.exists():
        base = configured_base
        free_probe = base
    else:
        base = Path(f"/var/tmp/lcsas-e2e-{os.getpid()}")
        env["LCSAS_E2E_BASE"] = str(base)
        cleanup_base = base
        # The base doesn't exist yet; probe its parent for free space.
        free_probe = base.parent

    free = shutil.disk_usage(free_probe).free
    if free < _MIN_FREE_BYTES:
        pytest.skip(
            f"need ~{_MIN_FREE_BYTES // (1024 * 1024)} MiB free at "
            f"{free_probe}, have {free // (1024 * 1024)} MiB"
        )

    try:
        if cleanup_base is not None:
            cleanup_base.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            [sys.executable, str(E2E_SCRIPT)],
            check=False,
            capture_output=True,
            env=env,
        )
        assert result.returncode == 0, _format_failure(result, E2E_SCRIPT)
    finally:
        if cleanup_base is not None and cleanup_base.exists():
            _force_rmtree(cleanup_base)


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
