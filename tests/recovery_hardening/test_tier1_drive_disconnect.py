"""Issue #222 — tier-1 must surface a useful error on drive read failure.

When a USB / cdemu optical drive disconnects mid-restore, every
pack we try to read from it will fail with EIO / ENXIO / ENOENT,
or — most commonly on real cdemu / mech-eject hardware — short
reads ending in unexpected EOF.  Tier-1 must:

  - Exit non-zero (not hang, not silently move on with a partial
    tree).
  - Print a diagnostic that identifies the failure as a source
    read error (not a target write error, not "pack not found"
    which sounds like a catalogue or planning bug).

The test simulates disc-loss by *truncating* the pack files of a
built rustic repo.  stat() still reports the original size (we
keep the directory entry intact), but pread() returns 0 before
delivering the requested bytes — exactly what a vanished USB
drive does to an in-flight read against a stale fd-or-inode.
lcsas_io.c maps that condition to errno=EIO, which the diagnostic
branch in lcsas_repo_read_blob catches.

Skipped when:
  - rustic is not on PATH (no way to build a fixture repo).
  - the lcsas-restore binary has not been built.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
)

pytestmark = pytest.mark.integration


def _truncate_all_packs(data_dir: Path) -> int:
    """Truncate every file under data_dir to zero length and return
    the count modified.  Walks two-level layout (data/<XX>/<id>)
    AND flat layout (data/<id>)."""
    n = 0
    for path in data_dir.rglob("*"):
        if path.is_file():
            with path.open("r+b") as f:
                f.truncate(0)
            n += 1
    return n


def test_source_pack_read_failure_reports_useful_error(tmp_path: Path) -> None:
    """A repo with its pack files truncated to zero length (but
    directory entries intact) must fail with a clear
    source-disc-read-failure diagnostic, not a bare 'pack not
    found' that sounds like a catalogue problem."""
    if shutil.which("rustic") is None:
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary built")

    src = tmp_path / "src"
    src.mkdir()
    # Several files so the snapshot definitely references at
    # least one content pack (small files can sometimes inline
    # into a tree blob, which the truncation wouldn't reach).
    for i in range(4):
        (src / f"file_{i}.bin").write_bytes(os.urandom(128 * 1024))

    pwfile = tmp_path / "pw"
    pwfile.write_text("drive-disconnect-test-pw\n")
    repo = tmp_path / "repo"
    build_rustic_repo(src, repo, pwfile)

    data_dir = repo / "data"
    assert data_dir.is_dir(), f"expected data/ in built repo at {repo}"
    n_truncated = _truncate_all_packs(data_dir)
    assert n_truncated > 0, "fixture didn't produce any pack files"

    target = tmp_path / "target"
    res = subprocess.run(
        [str(bin_path),
         "--repo", str(repo),
         "--target", str(target),
         "--password-file", str(pwfile),
         "--interactive", "off"],
        capture_output=True, text=True, timeout=120,
    )

    assert res.returncode != 0, (
        f"expected non-zero exit on source read failure; got 0\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    # The diagnostic must classify the failure as a source-side
    # read problem.  Three branches are acceptable: the open()-side
    # ENOENT/EIO/ENXIO/EBADF/EACCES classifier, the mid-pack pread
    # classifier, or the issue-#220 fstat short-circuit when the
    # truncation lands so that fstat sees a size shorter than the
    # index-declared end offset (the dominant path for zero-byte
    # truncation, since fstat catches it before pread).  All three
    # name the pack path so the operator can correlate with the
    # catalog and know which disc went bad.
    assert ("source disc read failed" in res.stderr
            or "pack truncated" in res.stderr), (
        f"expected source-side read-failure diagnostic; got:\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
