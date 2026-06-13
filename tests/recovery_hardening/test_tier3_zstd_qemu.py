"""Opt-in cross-arch proof: tier-3 zstd restore under aarch64 CPython (RST-04).

Runs the generated ``standalone_restorer.py`` under an aarch64 CPython via
``qemu-aarch64-static`` against a zstd-compressed fixture repo and asserts a
byte-identical restore — proving the pure-Python zstd decoder
(``lcsas.restore._zstd_pure``) works on a NON-host architecture with no
native ``zstandard`` present.

Mirrors the qemu-user pattern of ``test_tier1_aarch64_qemu.py``.  This is
opt-in and self-skipping: it needs both ``qemu-aarch64-static`` and an
aarch64 CPython interpreter.  Point the latter at a python3 binary with
``LCSAS_AARCH64_PYTHON`` (e.g. the ``python3`` inside a built meta-volume's
``recovery/bin/aarch64/python`` tree, or a python-build-standalone aarch64
extract).  CI wiring belongs to the GATE plans.

    LCSAS_ZSTD_QEMU=1 LCSAS_AARCH64_PYTHON=/path/to/aarch64/python3 \\
        pytest tests/recovery_hardening/test_tier3_zstd_qemu.py -v
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src"

_QEMU = shutil.which("qemu-aarch64-static")
_AARCH64_PY = os.environ.get("LCSAS_AARCH64_PYTHON")

pytestmark = pytest.mark.skipif(
    os.environ.get("LCSAS_ZSTD_QEMU") != "1"
    or _QEMU is None
    or not _AARCH64_PY
    or not Path(_AARCH64_PY).exists(),
    reason=(
        "opt-in: set LCSAS_ZSTD_QEMU=1 and LCSAS_AARCH64_PYTHON=<aarch64 "
        "python3>, with qemu-aarch64-static installed"
    ),
)


# Reuse the unit-test fixture builder for a zstd-compressed repo.
sys.path.insert(0, str(REPO_ROOT / "tests" / "unit"))


def _build_standalone(tmp_path: Path) -> Path:
    out = tmp_path / "standalone_restorer.py"
    code = (
        f"import sys; sys.path.insert(0, {str(SRC)!r});"
        "from lcsas.restore.standalone_builder import build_standalone;"
        f"import pathlib; pathlib.Path({str(out)!r})"
        ".write_text(build_standalone())"
    )
    res = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, timeout=60,
    )
    assert res.returncode == 0, res.stderr
    return out


def test_zstd_restore_under_aarch64_qemu(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")  # needed to PRODUCE the fixture repo
    from test_restic_fallback import (  # type: ignore[import-not-found]
        PASSWORD,
        _build_zstd_repo,
    )

    repo, original = _build_zstd_repo(tmp_path)
    script = _build_standalone(tmp_path)
    pw = tmp_path / "pw"
    pw.write_bytes(PASSWORD)
    target = tmp_path / "out"

    # Run the standalone restorer under the aarch64 interpreter via qemu.
    # An EMPTY PYTHONPATH + no-user-site guarantees no native zstandard is
    # importable, forcing the pure-Python backend.
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path / "noexist"),
        "PYTHONNOUSERSITE": "1",
        "LCSAS_PROGRESS": "0",
    }
    res = subprocess.run(
        [_QEMU, str(_AARCH64_PY), str(script),
         "--repo", str(repo),
         "--password-file", str(pw),
         "--target", str(target),
         "--interactive", "off"],
        capture_output=True, text=True, timeout=600, env=env,
    )
    assert res.returncode == 0, (
        f"aarch64 tier-3 restore failed:\nstdout:\n{res.stdout}\n"
        f"stderr:\n{res.stderr}"
    )
    restored = target / "big.txt"
    assert restored.is_file(), f"missing restored file; stderr:\n{res.stderr}"
    assert restored.read_bytes() == original
    # Belt-and-braces: hash equality.
    assert (
        hashlib.sha256(restored.read_bytes()).hexdigest()
        == hashlib.sha256(original).hexdigest()
    )
    # Sanity: it really used the slow pure path (no native zstandard).
    assert "slow built-in zstd" in res.stderr or "[restic-fallback]" in res.stderr


def test_fixture_repo_is_well_formed(tmp_path: Path) -> None:
    """Guard the fixture itself (runs whenever the suite isn't skipped):
    the data blob must carry uncompressed_length (the compress marker)."""
    pytest.importorskip("zstandard")
    from test_restic_fallback import (  # type: ignore[import-not-found]
        MASTER_ENCRYPT,
        MASTER_MAC_K,
        MASTER_MAC_R,
        PASSWORD,
        _build_zstd_repo,
    )

    repo, _ = _build_zstd_repo(tmp_path)
    # Decrypt the single index file and confirm a compressed blob exists.
    from lcsas.restore.restic_fallback import _decrypt_authenticated

    index_dir = repo / "index"
    idx_file = next(index_dir.iterdir())
    plaintext = _decrypt_authenticated(
        MASTER_ENCRYPT, MASTER_MAC_K, MASTER_MAC_R, idx_file.read_bytes()
    )
    doc = json.loads(plaintext)
    blobs = doc["packs"][0]["blobs"]
    assert any("uncompressed_length" in b for b in blobs)
    assert PASSWORD  # fixture exposes the password constant
