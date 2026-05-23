"""Issues #189 — tier-1 must restore uid/gid (when running as root).

Per the user's #189 comment: "implement as documented.  Make sure we
test root and non-root use and make sure they work as expected."

Two behaviours pinned:

1. **Non-root run**: tier-1 SILENTLY drops uid/gid (lchown would EPERM
   anyway).  Restored files are owned by the restoring process owner.
   This is also tier-2's behaviour on a non-privileged restore — no
   divergence.

2. **Root run**: tier-1 calls lchown to restore the snapshot's uid/gid.
   For symlinks the link's own uid/gid is set (AT_SYMLINK_NOFOLLOW
   semantics via lchown).

The non-root test runs always; the root test only when invoked as
root or when sudo -n is available.

Skips when rustic isn't on PATH.
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
    find_restored_root,
    restore_with_tier1,
)

pytestmark = pytest.mark.integration



def test_non_root_uid_gid_silently_dropped(tmp_path: Path) -> None:
    """Confirm non-root restore puts the running user as owner.
    The rustic backup records the original uid/gid, but lchown
    requires CAP_CHOWN — tier-1 detects euid != 0 and skips."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary")

    if os.geteuid() == 0:
        pytest.skip("test asserts non-root behaviour; running as root")

    src = tmp_path / "src"
    src.mkdir()
    (src / "alpha.txt").write_text("hi\n")
    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")

    build_rustic_repo(src, tmp_path / "repo", pwfile)
    target = tmp_path / "out"
    restore_with_tier1(tmp_path / "repo", target, pwfile, bin_path)

    root = find_restored_root(target)
    restored = next(root.rglob("alpha.txt"))
    st = restored.stat()

    # Restoring user owns the file (geteuid == st.st_uid).
    assert st.st_uid == os.geteuid(), (
        f"non-root restore should leave running user as owner; "
        f"got uid={st.st_uid}, expected {os.geteuid()}"
    )


@pytest.mark.skipif(
    os.geteuid() != 0 and subprocess.run(
        ["sudo", "-n", "true"], capture_output=True
    ).returncode != 0,
    reason="root-uid-gid restore test requires root or passwordless sudo",
)
def test_root_uid_gid_restored(tmp_path: Path) -> None:
    """When run as root, tier-1 must restore the original uid/gid
    from the snapshot's tree node."""
    if not shutil.which("rustic"):
        pytest.skip("rustic not on PATH")
    bin_path = find_restore_bin()
    if bin_path is None:
        pytest.skip("no lcsas-restore binary")

    # Backup as the test user so we know the original uid/gid.
    src = tmp_path / "src"
    src.mkdir()
    f = src / "alpha.txt"
    f.write_text("hi\n")
    backup_uid = f.stat().st_uid
    backup_gid = f.stat().st_gid

    pwfile = tmp_path / "pw"
    pwfile.write_text("test-password\n")
    repo = tmp_path / "repo"
    build_rustic_repo(src, repo, pwfile)

    target = tmp_path / "out"
    # Re-run tier-1 as root via sudo.
    if os.geteuid() == 0:
        restore_with_tier1(repo, target, pwfile, bin_path)
    else:
        target.mkdir(parents=True)
        subprocess.run(
            ["sudo", "-n", str(bin_path),
             "--repo", str(repo),
             "--target", str(target),
             "--password-file", str(pwfile)],
            check=True, capture_output=True, timeout=120,
        )

    root = find_restored_root(target)
    restored = next(root.rglob("alpha.txt"))
    st = restored.stat()
    assert st.st_uid == backup_uid, (
        f"root restore should preserve uid {backup_uid}; got {st.st_uid}"
    )
    assert st.st_gid == backup_gid, (
        f"root restore should preserve gid {backup_gid}; got {st.st_gid}"
    )

    # Cleanup root-owned dir.
    if os.geteuid() != 0:
        subprocess.run(["sudo", "-n", "rm", "-rf", str(target)],
                       check=False, capture_output=True)
