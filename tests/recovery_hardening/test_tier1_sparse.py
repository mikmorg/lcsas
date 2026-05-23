"""Issue #193 — tier-1 must preserve sparseness of restored files.

A 100 MiB file with most of it zero-filled should restore as a
sparse file (st_blocks << 100 MiB * 512 / block_size), not as a
fully-materialised dense file.

The implementation in tree.c scans each blob for runs of zero
bytes >= 4 KiB and `lseek`s past them, then `ftruncate`s the file
to its declared size so the trailing holes are reflected in the
size.

The test compares tier-1's restored st_blocks against tier-2's
(rustic restore).  If both end up similarly sparse, parity is
established.  If tier-1 is significantly denser, regression.

Skips when rustic isn't available or the test filesystem doesn't
honour sparse writes.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
    find_restored_root,
    restore_with_tier1,
    restore_with_tier2,
)

pytestmark = pytest.mark.integration



def _stat_blocks(path: Path) -> int:
    """Number of 512-byte blocks actually allocated to the file."""
    return path.stat().st_blocks


def test_sparse_file_restored_with_holes(tmp_path: Path) -> None:
    """A 4 MiB sparse source file (only 64 KiB of non-zero data near
    the end) must restore via tier-1 as roughly as sparse as the
    rustic-restore equivalent.

    "Sparse" here = st_blocks * 512 << logical size.  We assert
    tier-1's allocated bytes ≤ 4× tier-2's (allowing some slack for
    sub-4-KiB zero runs we don't bother holepunching)."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary")

    src = tmp_path / "src"
    src.mkdir()
    sparse = src / "vm.img"
    logical_size = 4 * 1024 * 1024  # 4 MiB
    # Write 64 KiB of non-zero data near the end of a 4-MiB file.
    with sparse.open("wb") as f:
        f.truncate(logical_size)
        f.seek(logical_size - 65536)
        f.write(os.urandom(65536))

    # Sanity-check the SOURCE is sparse — if not, this test's
    # filesystem doesn't honour holes and we can't make the
    # assertion.
    src_blocks = _stat_blocks(sparse)
    if src_blocks * 512 > logical_size // 2:
        pytest.skip(
            f"source filesystem materialised the file dense "
            f"(blocks={src_blocks}, logical_size={logical_size}); "
            "test can't assert sparseness parity"
        )

    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    repo = tmp_path / "repo"
    build_rustic_repo(src, repo, pwfile)

    # Tier-1 restore
    a = tmp_path / "tier1_out"
    restore_with_tier1(repo, a, pwfile, bin_path)
    a_root = find_restored_root(a)
    a_file = next(a_root.rglob("vm.img"))

    # Tier-2 restore
    b = tmp_path / "tier2_out"
    restore_with_tier2(repo, b, pwfile)
    b_root = find_restored_root(b)
    b_file = next(b_root.rglob("vm.img"))

    a_blocks = _stat_blocks(a_file)
    b_blocks = _stat_blocks(b_file)

    # Both should have the right logical size.
    assert a_file.stat().st_size == logical_size, (
        f"tier-1 logical size {a_file.stat().st_size} != {logical_size}"
    )
    assert b_file.stat().st_size == logical_size, (
        f"tier-2 logical size {b_file.stat().st_size} != {logical_size}"
    )

    # Tier-1 must be at MOST 4× tier-2's allocated blocks.  In the
    # ideal case both have ~64 KiB of allocated content; tier-1's
    # 4-KiB-grain hole detection might be slightly less efficient
    # than rustic's, hence the 4× tolerance.
    tolerance = max(b_blocks * 4, 4 * (logical_size // 512) // 100)
    assert a_blocks <= tolerance, (
        f"tier-1 restored file is much denser than tier-2: "
        f"tier1 blocks={a_blocks}, tier2 blocks={b_blocks}, "
        f"tolerance={tolerance}"
    )

    # And tier-1 must be meaningfully sparser than dense (the
    # whole point of the fix).  Dense would be logical_size//512
    # blocks (8192 for 4 MiB).  Sparse should be << that.
    dense_blocks = logical_size // 512
    assert a_blocks < dense_blocks // 4, (
        f"tier-1 restored file is not sparse: blocks={a_blocks}, "
        f"dense_blocks={dense_blocks}, threshold={dense_blocks // 4}"
    )
