"""Hardening: tier-1's index walks stay inside the parsed tokens (#487).

``repo.c``'s ``packs``, ``blobs`` and ``supersedes`` walks count only the
array elements they understand.  An element of another type kept
``found < count`` forever, and when that array was the last thing in the
file the walk ran past the parsed tokens: the zeroed spare slot has
``start == 0``, so the ``start >= end`` guard never stops it.  tree.c got
the same ``t < ntoks`` bound in T1C-04.

The index here is authenticated (encrypted with the repo's master key),
so it is "valid" to tier-1 in every way but shape.  The restore must
ignore the odd entries and succeed.  Under the ASan build
(``make -C recovery sanitize``) the old walk is a heap-buffer-overflow.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"
BINARY = Path(os.environ["LCSAS_RESTORE_BIN"]) if os.environ.get(
    "LCSAS_RESTORE_BIN"
) else RECOVERY_DIR / "build" / "lcsas-restore"

sys.path.insert(0, str(RECOVERY_DIR / "tests"))
import test_e2e  # noqa: E402  (sibling fixture builder)

from lcsas.restore.restic_fallback import _try_keys  # noqa: E402

PASSWORD = "walk-bounds"
FILES = {"a.txt": b"tier-1 index walk\n", "b.bin": os.urandom(4096)}


def _require_binary() -> None:
    if not (BINARY.is_file() and os.access(BINARY, os.X_OK)):
        pytest.skip(f"{BINARY} not built (make -C recovery)")


@pytest.mark.parametrize(
    "extra_index",
    [
        {"packs": [{"id": "0" * 64, "blobs": []}, 7]},
        {"packs": [{"id": "0" * 64, "blobs": [7]}]},
        {"packs": [], "supersedes": ["0" * 64, 7]},
        {"packs": [{"id": "0" * 64, "blobs": []}, "x", None, [1, [2]]]},
    ],
    ids=["packs-ends-in-number", "blobs-ends-in-number",
         "supersedes-ends-in-number", "packs-ends-in-mixed"],
)
def test_odd_index_elements_are_ignored(tmp_path: Path, extra_index: dict) -> None:
    _require_binary()
    repo = tmp_path / "repo"
    test_e2e.build_repo(repo, PASSWORD, FILES)
    mk = _try_keys(repo / "keys", PASSWORD.encode())
    enc = test_e2e.aead_encrypt(
        mk.encrypt, mk.mac_k, mk.mac_r, json.dumps(extra_index).encode())
    (repo / "index" / hashlib.sha256(enc).hexdigest()).write_bytes(enc)
    pw = tmp_path / "pw"
    pw.write_text(PASSWORD + "\n")
    target = tmp_path / "out"

    proc = subprocess.run(
        [str(BINARY), "--repo", str(repo), "--password-file", str(pw),
         "--target", str(target), "--snapshot", "latest",
         "--interactive", "off"],
        capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, (
        f"rc={proc.returncode} (negative = killed by a signal)\n{proc.stderr}"
    )
    restored = {p.name: p.read_bytes() for p in target.rglob("*") if p.is_file()}
    assert restored == FILES
