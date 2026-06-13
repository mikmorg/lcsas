"""T1C-01: one file with 40,000 content chunks (huge content array).

A single large file (e.g. a 60 GB disk image) is stored as tens of
thousands of 1-8 MB chunks; its tree node's ``content`` array is one
huge JSON array.  The old fixed 65536-token tree parser could not hold
such a node and aborted the whole restore.

``gen_fixture.py --chunks-per-file 40000`` emits one file node whose
content array repeats the (tiny) data-blob id 40,000 times — valid
restic semantics, a tiny on-disk fixture.  The restored file must equal
the payload concatenated 40,000 times.

Integration-marked (real binary, skipped when absent).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
GEN = REPO_ROOT / "recovery" / "tests" / "fixtures" / "gen_fixture.py"
RESTORE_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]

CHUNKS = 40000
PAYLOAD = b"hello from petabyte stress fixture\n"


def _find_bin() -> Path | None:
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        return p if p.is_file() and os.access(p, os.X_OK) else None
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def test_large_file_40000_chunks_restores(tmp_path: Path) -> None:
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    repo = tmp_path / "repo"
    target = tmp_path / "restored"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test")

    res = subprocess.run(
        ["python3", str(GEN), str(repo),
         "--stress", "0", "1", "1", "--chunks-per-file", str(CHUNKS)],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, f"fixture gen failed:\n{res.stderr[:2000]}"

    res = subprocess.run(
        [str(bin_path), "--repo", str(repo),
         "--target", str(target), "--password-file", str(pwfile)],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, (
        f"large-file restore failed (rc={res.returncode}); the old fixed "
        f"tree-token cap could not hold a 40000-element content array.\n"
        f"stderr tail:\n{res.stderr[-2000:]}"
    )
    assert "tree restore failed" not in res.stderr, res.stderr[-2000:]

    restored = sorted(target.rglob("file_*.txt"))
    assert len(restored) == 1, f"expected 1 file, got {len(restored)}"
    out = restored[0]
    assert out.stat().st_size == len(PAYLOAD) * CHUNKS, (
        f"size mismatch: {out.stat().st_size} != {len(PAYLOAD) * CHUNKS}"
    )
    # Hash-compare against payload * CHUNKS without holding it twice in RAM.
    want = hashlib.sha256(PAYLOAD * CHUNKS).hexdigest()
    h = hashlib.sha256()
    with out.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    assert h.hexdigest() == want, "restored content hash mismatch"
