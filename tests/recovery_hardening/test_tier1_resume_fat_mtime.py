"""Tier 1 resume skips a file whose mtime is off by less than 2 s (#787).

Resume skips a file whose size and mtime match the snapshot.  FAT keeps
times in 2-second steps, so on a FAT or exFAT stick a file with an
odd-second mtime comes back 1 s off and was restored again on every
resume.  Tier 1 now accepts a difference of less than 2 s; a file 2 s or
more off is still restored again (#661: it may be another snapshot's).

No FAT file system is needed: the test moves the restored file's mtime
by hand, as FAT's rounding would.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests.recovery_hardening.test_tier1_fault_handling import (
    _find_bin,
    _require_fixture,
    _run,
)


def _restore(bin_path: Path, repo: Path, pw: Path, target: Path) -> str:
    res = _run(bin_path, "--repo", str(repo), "--password-file", str(pw),
               "--target", str(target), "--interactive", "off", timeout=60)
    assert res.returncode in (0, 1), res.stderr
    assert (target / "hello.txt").is_file(), res.stderr
    return res.stderr


@pytest.mark.parametrize(
    ("offset", "skipped"),
    [(0, True), (1, True), (-1, True), (2, False), (-3, False)],
)
def test_resume_tolerates_fat_mtime_rounding(
    tmp_path: Path, offset: int, skipped: bool,
) -> None:
    bin_path = _find_bin()
    repo = _require_fixture()
    pw = tmp_path / "pw"
    pw.write_bytes(b"test\n")
    target = tmp_path / "out"
    _restore(bin_path, repo, pw, target)
    hello = target / "hello.txt"
    st = hello.stat()
    os.utime(hello, (st.st_atime, st.st_mtime + offset))

    stderr = _restore(bin_path, repo, pw, target)
    line = f"skipping already-restored: {hello}"
    assert (line in stderr) is skipped, stderr
    # Restored again or not, the file ends with the snapshot's mtime
    # (or the one within the tolerance that was kept).
    assert abs(hello.stat().st_mtime - st.st_mtime) < 2
