"""Issue #188 — tier-1 must preserve file/directory mtime.

Before this fix, every file restored by `lcsas-restore` got the
restore-time clock as its mtime.  That makes restored trees look
"freshly modified", which breaks build systems, rsync, and forensic
timelines.

This test backs up a small tree whose files have known historic
mtimes via `rustic backup`, then restores via both tier-1 and tier-2
(rustic) and asserts:

  - tier-1 mtime is within a small tolerance of the original (we
    don't try for sub-second parity; some filesystems strip ns
    precision);
  - tier-1 and tier-2 mtimes match each other (the durability promise
    of tier-1 is parity with tier-2).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    diff_trees,
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

# Tolerance: rustic emits ns-precision strings but the kernel/filesystem
# round-trip can shave sub-second bits, and `os.utime` only takes float
# seconds anyway.  A ±2 s window is generous enough to absorb any of
# that and still catch the original bug (which would land mtime in
# 2026, decades from the chosen test epochs).
MTIME_TOLERANCE_SEC = 2.0

# A spread of timestamps far enough apart, and far enough from "now",
# that a regression to restore-time mtime is impossible to mistake for
# noise.
ORIGINAL_MTIMES = {
    "old.txt": 1_577_836_800,    # 2020-01-01T00:00:00Z
    "newer.txt": 1_704_067_200,  # 2024-01-01T00:00:00Z
    "ancient.txt": 946_684_800,  # 2000-01-01T00:00:00Z
}


def _find_restore_bin() -> Path | None:
    for p in RESTORE_BIN_CANDIDATES:
        if p.is_file() and os.access(p, os.X_OK):
            return p
    return None


def _find_restored_root(target: Path) -> Path:
    """Mirror of the helper in test_tier1_vs_tier2_differential.py:
    restorers may nest the restored tree under the original absolute
    source path.  Walk down single-entry directories to find the
    effective root."""
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


def test_tier1_preserves_mtime(tmp_path: Path) -> None:
    """Round-trip through rustic backup + tier-1 restore must keep
    the original mtime within MTIME_TOLERANCE_SEC."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = _find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")

    src = tmp_path / "src"
    src.mkdir()
    for name, mtime in ORIGINAL_MTIMES.items():
        f = src / name
        f.write_text(f"content of {name}\n")
        os.utime(f, (mtime, mtime))

    repo = tmp_path / "repo"
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    build_rustic_repo(src, repo, pwfile)

    tier1 = tmp_path / "tier1_out"
    tier2 = tmp_path / "tier2_out"
    restore_with_tier1(repo, tier1, pwfile, bin_path)
    restore_with_tier2(repo, tier2, pwfile)

    tier1_root = _find_restored_root(tier1)
    tier2_root = _find_restored_root(tier2)

    failures: list[str] = []
    for name, expected in ORIGINAL_MTIMES.items():
        p1 = tier1_root / name
        p2 = tier2_root / name
        if not p1.exists():
            failures.append(f"{name}: missing from tier-1 output")
            continue
        if not p2.exists():
            failures.append(f"{name}: missing from tier-2 output")
            continue
        m1 = p1.stat().st_mtime
        m2 = p2.stat().st_mtime
        if abs(m1 - expected) > MTIME_TOLERANCE_SEC:
            failures.append(
                f"{name}: tier-1 mtime {m1} differs from original "
                f"{expected} by more than ±{MTIME_TOLERANCE_SEC}s "
                f"(diff={m1 - expected:+.2f}s)"
            )
        if abs(m1 - m2) > MTIME_TOLERANCE_SEC:
            failures.append(
                f"{name}: tier-1 mtime {m1} differs from tier-2 mtime "
                f"{m2} by more than ±{MTIME_TOLERANCE_SEC}s"
            )

    assert not failures, "\n".join(failures)

    # Also exercise the diff_trees mtime path (added in this PR) — gives
    # us a regression net on the helper itself, not just on tree.c.
    diffs = diff_trees(tier1_root, tier2_root, compare_mtime=True)
    assert not diffs, (
        "diff_trees(compare_mtime=True) reports tier-1 vs tier-2 "
        "divergence:\n" + "\n".join(diffs)
    )
