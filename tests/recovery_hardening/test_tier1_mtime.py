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
    find_restore_bin,
    find_restored_root,
    restore_with_tier1,
    restore_with_tier2,
)

pytestmark = pytest.mark.integration


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


def test_tier1_preserves_mtime(tmp_path: Path) -> None:
    """Round-trip through rustic backup + tier-1 restore must keep
    the original mtime within MTIME_TOLERANCE_SEC."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
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

    tier1_root = find_restored_root(tier1)
    tier2_root = find_restored_root(tier2)

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
