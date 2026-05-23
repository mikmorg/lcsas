"""Issue #192 -- tier-1 hardlink reconstruction.

Restic stores hardlinks via a per-node ``"inode"`` JSON field; two file
nodes sharing the same inode are the same hardlinked file.  Before
issue #192 was fixed, tier-1 (``lcsas-restore``) wrote each name as a
fresh content copy, blowing up restore size by the link factor for
trees like ``/usr/lib/firmware``.  Tier-2 (``rustic restore``) has
always reconstructed hardlinks via ``link(2)``.

This test builds a source containing three hardlinked names backed by
a single inode, backs it up with rustic, restores via both tiers, and
asserts:

  1. All three names share one inode after tier-1 restore.
  2. All three names share one inode after tier-2 restore.
  3. Content is identical across tiers.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    restore_with_tier1,
    restore_with_tier2,
)

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_BIN_CANDIDATES = [
    REPO_ROOT / "recovery" / "build" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64-linux-musl" / "lcsas-restore",
    REPO_ROOT / "recovery" / "bin" / "x86_64" / "lcsas-restore",
]


def _find_restore_bin() -> Path | None:
    for p in RESTORE_BIN_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def _find_restored_root(target: Path) -> Path:
    """Both restorers may place the restored tree under
    ``<target>/<abs_src_path>/``.  Walk down until we hit a directory
    with more than one entry to find the effective tree root."""
    cur = target
    while True:
        try:
            entries = list(cur.iterdir())
        except FileNotFoundError:
            return target
        if len(entries) != 1:
            return cur
        only = entries[0]
        if not only.is_dir() or only.is_symlink():
            return cur
        cur = only


def test_tier1_reconstructs_hardlinks(tmp_path: Path) -> None:
    """Three hardlinked source files must restore to three names
    sharing one inode under BOTH tier-1 and tier-2."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = _find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    original = src / "original.txt"
    original.write_text("shared content\n")
    os.link(original, src / "hardlink_a.txt")
    os.link(original, src / "hardlink_b.txt")

    # Sanity check: the source files share an inode.
    src_ino = original.stat().st_ino
    assert (src / "hardlink_a.txt").stat().st_ino == src_ino
    assert (src / "hardlink_b.txt").stat().st_ino == src_ino

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    build_rustic_repo(src, repo, pwfile)

    tier1_out = tmp_path / "tier1_out"
    tier2_out = tmp_path / "tier2_out"
    restore_with_tier1(repo, tier1_out, pwfile, bin_path)
    restore_with_tier2(repo, tier2_out, pwfile)

    tier1_root = _find_restored_root(tier1_out)
    tier2_root = _find_restored_root(tier2_out)

    names = ("original.txt", "hardlink_a.txt", "hardlink_b.txt")

    # All three restored files must exist on both tiers.
    for tag, root in (("tier1", tier1_root), ("tier2", tier2_root)):
        for n in names:
            assert (root / n).is_file(), f"{tag} missing {n}"

    # All three names must share a single inode on tier-1 (the bug).
    t1_inos = {n: (tier1_root / n).stat().st_ino for n in names}
    assert len(set(t1_inos.values())) == 1, (
        f"tier-1 failed to reconstruct hardlinks; inodes: {t1_inos}"
    )

    # Same for tier-2 (the reference).
    t2_inos = {n: (tier2_root / n).stat().st_ino for n in names}
    assert len(set(t2_inos.values())) == 1, (
        f"tier-2 unexpectedly failed to reconstruct hardlinks; "
        f"inodes: {t2_inos}"
    )

    # Content must match across all six restored files.
    expected = original.read_bytes()
    for tag, root in (("tier1", tier1_root), ("tier2", tier2_root)):
        for n in names:
            got = (root / n).read_bytes()
            assert got == expected, f"{tag}/{n}: content mismatch"
