"""T1C-01: one dense index file the old fixed-cap parser could not read.

The legacy tier-1 parser allocated a fixed 32768-token buffer for the
index pass-2 and SILENTLY skipped any file that overflowed it — every
blob that file described was dropped, and the restore later died with a
cryptic ``blob not in index``.  The petabyte fixture sidesteps this by
splitting orphans across many small index files (3000 entries each), so
no existing test ever feeds one dense index.

This test writes 6,000 blob entries into ONE index file (~60k tokens,
well past the old cap) via ``gen_fixture.py --dense-index`` and asserts
the real files restore byte-identical AND that stderr carries none of
the generic/cryptic failure strings.

Integration-marked (real binary, skipped when absent).
"""

from __future__ import annotations

import json
import os
import shutil
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


def _gen(out: Path, *extra: str, n_orphans: int, n_files: int,
         n_subdirs: int) -> dict:
    if out.exists():
        shutil.rmtree(out)
    res = subprocess.run(
        ["python3", str(GEN), str(out),
         "--stress", str(n_orphans), str(n_files), str(n_subdirs), *extra],
        capture_output=True, text=True, timeout=300,
    )
    assert res.returncode == 0, f"fixture gen failed:\n{res.stderr[:2000]}"
    return json.loads((out / "manifest.json").read_text())


def _restore(bin_path: Path, repo: Path, target: Path,
             pwfile: Path) -> subprocess.CompletedProcess:
    pwfile.write_text("test")
    if target.exists():
        shutil.rmtree(target)
    return subprocess.run(
        [str(bin_path), "--repo", str(repo),
         "--target", str(target), "--password-file", str(pwfile)],
        capture_output=True, text=True, timeout=300,
    )


CRYPTIC = ("tree restore failed", "blob not in index", "index load failed")


def test_dense_index_6000_entries_restores_byte_identical(tmp_path: Path) -> None:
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    repo = tmp_path / "repo"
    target = tmp_path / "restored"
    pwfile = tmp_path / "pw"

    _gen(repo, "--dense-index", n_orphans=6000, n_files=3, n_subdirs=1)
    res = _restore(bin_path, repo, target, pwfile)

    assert res.returncode == 0, (
        f"dense-index restore failed (rc={res.returncode}); the old "
        f"fixed-cap parser would silently drop the 6000-entry index.\n"
        f"stderr tail:\n{res.stderr[-2000:]}"
    )
    for marker in CRYPTIC:
        assert marker not in res.stderr, (
            f"restore emitted the cryptic failure {marker!r} — a dense "
            f"index must parse cleanly, not regress to a generic abort.\n"
            f"stderr:\n{res.stderr[-2000:]}"
        )

    restored = sorted(target.rglob("file_*.txt"))
    assert len(restored) == 3, f"expected 3 files, got {len(restored)}"
    payload = b"hello from petabyte stress fixture\n"
    for f in restored:
        assert f.read_bytes() == payload, f"content mismatch: {f}"


def test_clamped_ceiling_fails_loud_naming_the_index(tmp_path: Path) -> None:
    """LCSAS_MAX_JSON_MIB=1 against a fixture that exceeds it must exit
    non-zero, name the offending index file, and point at tier-2 — never
    a bare 'tree restore failed' or 'blob not in index'."""
    bin_path = _find_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    repo = tmp_path / "repo"
    target = tmp_path / "restored"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test")

    _gen(repo, "--dense-index", n_orphans=6000, n_files=3, n_subdirs=1)
    if target.exists():
        shutil.rmtree(target)

    env = dict(os.environ, LCSAS_MAX_JSON_MIB="1")
    res = subprocess.run(
        [str(bin_path), "--repo", str(repo),
         "--target", str(target), "--password-file", str(pwfile)],
        capture_output=True, text=True, timeout=300, env=env,
    )

    assert res.returncode != 0, "clamped ceiling should fail, not succeed"
    assert "too large for tier-1" in res.stderr, res.stderr[-2000:]
    assert "use tier-2 (rustic)" in res.stderr, res.stderr[-2000:]
    assert "index file" in res.stderr, res.stderr[-2000:]
    for marker in ("tree restore failed", "blob not in index"):
        assert marker not in res.stderr, (
            f"clamped-ceiling abort must be diagnosable, not {marker!r}\n"
            f"{res.stderr[-2000:]}"
        )
