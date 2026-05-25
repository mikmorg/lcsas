"""Issue #224 — tier-1 must degrade gracefully on a FAT32 target.

Operators sometimes restore to a USB drive formatted FAT32 / exFAT.
Those filesystems cannot store POSIX symlinks, POSIX modes
(setuid/setgid/sticky), uid/gid, or xattrs.  Tier-1's call surface
for each of these is best-effort -- but the symlink path was
previously emitting a generic "symlink failed" line that read like a
real bug.  This test pins:

  * The restore completes (exit 0), not crashes.
  * Regular files are present and content-correct.
  * Each unsupported symlink gets a per-node FAT32-aware WARNING.

The other dimensions (chmod, lchown, lsetxattr) are already
no-op-on-failure in the C code (`(void)` casts everywhere; see
tree.c §apply_node_ownership, etc.) so this test deliberately
focuses on symlinks -- the one node type that previously surfaced
as a noisy "failed" line.

Skipped unless run as root (or with passwordless sudo) because
loop-mounting needs CAP_SYS_ADMIN.  Marked ``integration`` because
it relies on rustic + mkfs.vfat + loop devices being present.
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


def _have_sudo() -> bool:
    if os.geteuid() == 0:
        return True
    return subprocess.run(
        ["sudo", "-n", "true"], capture_output=True
    ).returncode == 0


def _sudo(*argv: str, check: bool = True,
          capture_output: bool = True,
          ) -> subprocess.CompletedProcess:
    """Run a command as root, prepending ``sudo -n`` iff we're not."""
    cmd = list(argv) if os.geteuid() == 0 else ["sudo", "-n", *argv]
    return subprocess.run(cmd, check=check, capture_output=capture_output,
                          text=True, timeout=60)


@pytest.mark.skipif(not _have_sudo(),
                    reason="FAT32 loop-mount requires root / passwordless sudo")
@pytest.mark.skipif(not shutil.which("mkfs.vfat"),
                    reason="mkfs.vfat not installed")
@pytest.mark.skipif(not shutil.which("rustic"),
                    reason="rustic not on PATH")
def test_fat32_target_completes_with_symlink_warning(tmp_path: Path) -> None:
    """Restore a small snapshot containing a symlink + a setuid file
    onto a FAT32 loop-mount.  Assert: exit 0, regular files restored
    correctly, per-node FAT32 warning printed for the symlink."""
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary")

    # Build a small source tree with a regular file, a setuid file,
    # and a symlink -- every node type that exercises the FS-cap
    # mismatch surface.
    src = tmp_path / "src"
    src.mkdir()
    (src / "plain.txt").write_text("hello fat32\n")
    setuid_file = src / "setuid.bin"
    setuid_file.write_bytes(b"binary\n")
    setuid_file.chmod(0o4755)  # setuid+rwxr-xr-x
    (src / "link").symlink_to("plain.txt")

    pwfile = tmp_path / "pw"
    pwfile.write_text("fat32-test-pw\n")
    repo = tmp_path / "repo"
    build_rustic_repo(src, repo, pwfile)

    # Build a 16 MiB FAT32 image and loop-mount it as the target dir.
    img = tmp_path / "fat32.img"
    with img.open("wb") as f:
        f.truncate(16 * 1024 * 1024)
    subprocess.run(["mkfs.vfat", "-F", "32", str(img)],
                   check=True, capture_output=True, timeout=30)
    target = tmp_path / "fat32_target"
    target.mkdir()
    _sudo("mount", "-o", f"loop,uid={os.geteuid()}",
          str(img), str(target))
    try:
        # The mount-time uid= option grants the running user write
        # access; lcsas-restore can then run without sudo.
        res = subprocess.run(
            [str(bin_path),
             "--repo", str(repo),
             "--target", str(target),
             "--password-file", str(pwfile)],
            capture_output=True, text=True, timeout=120,
        )

        assert res.returncode == 0, (
            f"restore must complete on a FAT32 target; "
            f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
        )

        # The regular files made it across.  rglob covers the
        # "restore-under-abs-source-path" convention without us
        # hard-coding the layout.
        plain_hits = list(target.rglob("plain.txt"))
        assert plain_hits, (
            f"plain.txt missing from FAT32 restore; "
            f"contents:\n{list(target.rglob('*'))}"
        )
        assert plain_hits[0].read_text() == "hello fat32\n"

        # The symlink was the lossy node.  Tier-1 prints a per-node
        # FAT32-aware warning instead of the generic "symlink failed"
        # banner -- this is the operator-visible signal that the
        # target FS can't store the symlink semantically.
        assert "does not support symlinks" in res.stderr, (
            f"missing FAT32 symlink WARNING; stderr was:\n{res.stderr}"
        )
        # The generic "symlink failed:" line must NOT appear -- that
        # was the pre-#224 message and reads like a real bug.
        assert "symlink failed:" not in res.stderr, (
            f"generic 'symlink failed' must be replaced with the "
            f"FS-capability warning; stderr was:\n{res.stderr}"
        )
    finally:
        # Unmount eagerly and SYNCHRONOUSLY so pytest's tmp_path
        # rmtree doesn't trip over a busy loop-mount.  -l (lazy) is
        # the fallback in case anything in the test held the mount.
        umount = _sudo("umount", str(target), check=False)
        if umount.returncode != 0:
            _sudo("umount", "-l", str(target), check=False)
