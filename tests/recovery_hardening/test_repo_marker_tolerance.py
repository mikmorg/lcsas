"""test_repo_marker_tolerance.py -- the real readers ignore LCSAS_REPO.txt.

WHY THIS GATE EXISTS
--------------------
rc6 (contract C1) puts a backup set marker, ``LCSAS_REPO.txt``, inside
every ``metadata/<repo_id>/`` folder on every disc.  That folder is the
root of the restic repository that the readers open: tier 1 with
``--repo``, tier 2 (rustic-static) with ``--repository``, tier 2b and the
standard-tools runbook (stock restic) with ``-r``.  Restore must work
exactly as before with the extra file there.

This gate proves it with the real binaries.  A rustic-written repository
carries a marker (with a hostile name and a split), and

  * rustic restores it byte for byte, and ``rustic check`` passes;
  * stock restic restores it byte for byte, and ``restic check`` passes;
  * the committed tier-1 ``lcsas-restore`` (Linux x86_64) restores it
    byte for byte.

The tier-3 Python reader (``restic_fallback`` and the generated
``standalone_restorer.py``) is checked in ``tests/unit/test_repo_marker.py``.

Each test skips when its binary is absent (``rustic`` / ``restic`` on
PATH, or ``LCSAS_RESTIC_BIN`` for stock restic).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.db.models import KeyEscrow
from lcsas.staging.repo_marker import (
    MARKER_FILENAME,
    make_repo_marker,
    read_repo_marker,
    write_repo_marker,
)
from tests.recovery_hardening._diff_helpers import diff_trees

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
REPO_ID = "3f2b8c1e-9a4d-4e7b-b6c2-0d5e7f1a2b3c"


def _stock_restic() -> str | None:
    """Path to a STOCK upstream restic binary (not rustic, not bundled)."""
    return os.environ.get("LCSAS_RESTIC_BIN") or shutil.which("restic")


def _tier1_bin() -> Path | None:
    """The committed tier-1 binary, when it can run on this host."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        return None
    path = REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore"
    return path if os.access(path, os.X_OK) else None


requires_rustic = pytest.mark.skipif(
    shutil.which("rustic") is None, reason="rustic not installed"
)
requires_stock_restic = pytest.mark.skipif(
    _stock_restic() is None,
    reason="stock restic not installed (set LCSAS_RESTIC_BIN or put it on PATH)",
)
requires_tier1 = pytest.mark.skipif(
    _tier1_bin() is None,
    reason="committed tier-1 lcsas-restore runs only on Linux x86_64",
)


def _rustic(repo: Path, pwfile: Path, *args: str, cwd: Path) -> None:
    subprocess.run(
        ["rustic", "--no-cache", "-r", str(repo), "--password-file", str(pwfile),
         *args],
        cwd=str(cwd), capture_output=True, check=True, timeout=180,
    )


@pytest.fixture(scope="module")
def marked_repo(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, Path]:
    """A rustic-written repository with a marker at its root.

    Returns (repo, password file, source tree).  The repository folder is
    named by the repo_id, as on a disc.
    """
    if shutil.which("rustic") is None:
        pytest.skip("rustic not installed")
    base = tmp_path_factory.mktemp("marker")
    src = base / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("hello from lcsas\n")
    (src / "sub" / "b.bin").write_bytes(bytes(range(256)) * 64)  # 16 KiB
    (src / "sub" / "note.md").write_text("nested text\n")
    pwfile = base / "key.txt"
    pwfile.write_text("pw-correct-horse\n")
    repo = base / REPO_ID

    # Back up a RELATIVE path so every restore reproduces <target>/src.
    _rustic(repo, pwfile, "init", cwd=base)
    _rustic(repo, pwfile, "backup", "src", cwd=base)

    marker = make_repo_marker(
        REPO_ID,
        config_name='photos "2026" & <more> | 100%!\n^',
        record=KeyEscrow(
            repo_id=REPO_ID, threshold=2, shares=3, slip39_id=4242,
            split_at="2026-11-03T00:00:00+00:00",
        ),
    )
    write_repo_marker(repo, marker)
    fields = read_repo_marker(repo)
    assert fields is not None and fields["split"] == "2-of-3"
    return repo, pwfile, src


def _assert_restored(src: Path, out: Path, who: str) -> None:
    restored = out / "src"
    assert restored.is_dir(), (
        f"{who} produced no src/ tree under {out}; got "
        f"{sorted(p.name for p in out.rglob('*'))[:20]}"
    )
    diffs = diff_trees(src, restored)
    assert not diffs, (
        f"{who} did not restore byte for byte with {MARKER_FILENAME} at the "
        f"repository root: {diffs}"
    )


@requires_rustic
def test_rustic_restores_and_checks_with_the_marker(
    marked_repo: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    repo, pwfile, src = marked_repo
    out = tmp_path / "rustic-out"
    # The tier-2 command line of restore.sh (rustic-static is this binary).
    res = subprocess.run(
        ["rustic", "--no-cache", "--repository", str(repo),
         "--password-file", str(pwfile), "restore", "latest", str(out)],
        capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stderr
    _assert_restored(src, out, "rustic")

    res = subprocess.run(
        ["rustic", "--no-cache", "-r", str(repo),
         "--password-file", str(pwfile), "check"],
        capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stderr
    assert "[ERROR]" not in res.stderr, res.stderr


@requires_stock_restic
def test_stock_restic_restores_and_checks_with_the_marker(
    marked_repo: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    repo, pwfile, src = marked_repo
    restic = str(_stock_restic())
    env = {**os.environ, "RESTIC_PASSWORD_FILE": str(pwfile)}
    out = tmp_path / "restic-out"
    # The tier-2b command line of restore.sh.
    res = subprocess.run(
        [restic, "--no-cache", "-r", str(repo), "restore", "latest",
         "--target", str(out), "--no-lock"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert res.returncode == 0, res.stderr
    _assert_restored(src, out, "stock restic")

    res = subprocess.run(
        [restic, "--no-cache", "-r", str(repo), "check", "--no-lock"],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert res.returncode == 0, res.stderr
    assert "no errors were found" in res.stdout, res.stdout


@requires_tier1
def test_tier1_restores_with_the_marker(
    marked_repo: tuple[Path, Path, Path], tmp_path: Path,
) -> None:
    repo, pwfile, src = marked_repo
    out = tmp_path / "tier1-out"
    res = subprocess.run(
        [str(_tier1_bin()), "--repo", str(repo), "--password-file", str(pwfile),
         "--target", str(out), "--interactive", "off"],
        capture_output=True, text=True, timeout=180,
    )
    assert res.returncode == 0, res.stderr
    _assert_restored(src, out, "tier-1 lcsas-restore")
