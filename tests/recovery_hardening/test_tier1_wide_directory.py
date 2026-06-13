"""T1C-01: one very wide directory (5,000 entries in a single tree blob).

The old tree.c parser allocated a fixed 65536-token buffer; a directory
with very many entries overflowed it and aborted the whole restore with
the generic ``tree restore failed`` — no hint that a big folder was the
cause and that tier-2 would have succeeded.

``make_stress_fixture(n_files=5000, n_subdirs=1)`` packs all 5,000 file
nodes into ONE sub-tree blob (>100k tokens).  With the adaptive parser
this must restore every file byte-identical.

Integration-marked (real binary, skipped when absent).
"""

from __future__ import annotations

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


def _find_bin() -> Path | None:
    if path := os.environ.get("LCSAS_RESTORE_BIN"):
        p = Path(path)
        return p if p.is_file() and os.access(p, os.X_OK) else None
    for p in RESTORE_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def test_wide_directory_5000_entries_restores(tmp_path: Path) -> None:
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    repo = tmp_path / "repo"
    target = tmp_path / "restored"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test")

    res = subprocess.run(
        ["python3", str(GEN), str(repo), "--stress", "0", "5000", "1"],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, f"fixture gen failed:\n{res.stderr[:2000]}"

    res = subprocess.run(
        [str(bin_path), "--repo", str(repo),
         "--target", str(target), "--password-file", str(pwfile)],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, (
        f"wide-directory restore failed (rc={res.returncode}); the old "
        f"65536-token tree parser would abort with 'tree restore failed'.\n"
        f"stderr tail:\n{res.stderr[-2000:]}"
    )
    assert "tree restore failed" not in res.stderr, res.stderr[-2000:]

    restored = sorted(target.rglob("file_*.txt"))
    assert len(restored) == 5000, f"expected 5000 files, got {len(restored)}"
    payload = b"hello from petabyte stress fixture\n"
    # Spot-check every file is byte-identical (cheap: 5000 small files).
    for f in restored:
        assert f.read_bytes() == payload, f"content mismatch: {f}"
