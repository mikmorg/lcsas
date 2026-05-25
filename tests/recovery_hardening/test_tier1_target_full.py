"""Issue #221 — tier-1 must surface ENOSPC on the target filesystem.

When the operator's --target directory fills up mid-restore the
binary should emit a clear "out of space" diagnostic rather than a
generic "file restore failed" line.  Without this, the operator
sees a half-written tree and no obvious cause.

The test mounts a tiny tmpfs (1 MiB) as the target directory and
restores a snapshot containing a single file larger than that.
Asserts:

  - Non-zero exit code.
  - stderr contains the specific "out of space" diagnostic with
    the target path.

Skipped when:
  - rustic is not on PATH (no way to build a fixture repo).
  - the lcsas-restore binary has not been built.
  - the harness can't `sudo mount`/`umount` a tmpfs (no
    passwordless sudo, no `mount`/`umount` on PATH).
"""
from __future__ import annotations

import contextlib
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


def _can_sudo_mount() -> bool:
    """True when we can mount/umount a tmpfs without prompting."""
    if shutil.which("mount") is None or shutil.which("umount") is None:
        return False
    res = subprocess.run(
        ["sudo", "-n", "true"], capture_output=True, timeout=5,
    )
    return res.returncode == 0


def test_target_dir_out_of_space_reports_enospc(tmp_path: Path) -> None:
    """A 1 MiB tmpfs --target with a 4 MiB file in the snapshot
    must fail with a clear ENOSPC message naming the target path,
    not a generic 'file restore failed'."""
    if shutil.which("rustic") is None:
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary built")
    if not _can_sudo_mount():
        pytest.skip("passwordless sudo + mount/umount required")

    # Build a fixture repo containing a single 4 MiB file.  Random
    # bytes prevent zstd from squashing it well below 1 MiB
    # post-compression and slipping under the tmpfs cap.
    src = tmp_path / "src"
    src.mkdir()
    big = src / "big.bin"
    big.write_bytes(os.urandom(4 * 1024 * 1024))

    pwfile = tmp_path / "pw"
    pwfile.write_text("target-full-test-pw\n")
    repo = tmp_path / "repo"
    build_rustic_repo(src, repo, pwfile)

    # Use a unique mount point so concurrent test runs don't fight
    # over /tmp/lcsas-tinytmp.  Created and torn down in a finally
    # block; size=1m is the smallest tmpfs size the kernel honours.
    mnt = Path(f"/tmp/lcsas-tinytmp-{os.getpid()}")
    mnt.mkdir(parents=True, exist_ok=True)

    try:
        subprocess.run(
            ["sudo", "mount", "-t", "tmpfs",
             "-o", f"size=1m,uid={os.getuid()},gid={os.getgid()},mode=0700",
             "tmpfs", str(mnt)],
            check=True, capture_output=True, timeout=10,
        )

        # Should fail.  We don't pin a specific non-zero exit code —
        # the binary uses `1` for any error today, but the contract
        # is "non-zero".  The diagnostic line is the load-bearing
        # part.
        res = subprocess.run(
            [str(bin_path),
             "--repo", str(repo),
             "--target", str(mnt),
             "--password-file", str(pwfile),
             "--interactive", "off"],
            capture_output=True, text=True, timeout=120,
        )

        assert res.returncode != 0, (
            f"expected non-zero exit on ENOSPC; got 0\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )
        assert "out of space" in res.stderr, (
            f"expected 'out of space' diagnostic; got:\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )
        # The diagnostic should name the target directory so the
        # operator can identify *which* mount filled up when several
        # restores run in parallel.
        assert str(mnt) in res.stderr, (
            f"expected target path {mnt} in stderr; got:\n{res.stderr}"
        )
    finally:
        # Best-effort cleanup.  `-l` makes umount tolerant of
        # leftover open fds the restore left behind.
        subprocess.run(
            ["sudo", "umount", "-l", str(mnt)],
            capture_output=True, timeout=10,
        )
        with contextlib.suppress(OSError):
            mnt.rmdir()
