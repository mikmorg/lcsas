"""Hardening test: tier-1 meta-disc exclusion respects a LIVE check.

Issue #143: ``recovery/src/lcsas-restore/disc_locator.c`` used to blacklist
every path under ``--meta-disc`` (and every child) for the lifetime of
the binary.  After the operator ejected the meta disc and remounted a
data disc at the SAME mount point (``/mnt`` — the natural choice on a
single-drive box), the binary refused to consider that mount point as
a pack source and the swap prompt looped forever.

The fix replaces the static ``path_under(p, meta)`` exclusion with a
live sentinel probe (``meta_disc_is_live`` — checks for
``<meta>/recovery/scripts/restore.sh``).  When the sentinel is gone
the exclusion is silently disabled and the operator's data disc at
``/mnt`` is treated like any other pack source.

This is the integration-level smoke for that behaviour; the
load-bearing assertion is in ``recovery/tests/test_disc_locator.c``
(the C unit test).  This test confirms the wiring in the actual
binary by running it against synthetic disc fixtures.

Single test: build a real (rustic-format) split fixture, put the
meta-sentinel at the same path as the data disc, then remove it,
and assert the binary completes a restore both times (the
restore must succeed END-TO-END only in the second case; in the
first case the locator should NOT see the same path as a pack
source — but we don't have to assert that explicitly, the C unit
test does).  Here we just verify the binary doesn't regress on
the post-eject path.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_DIR = REPO_ROOT / "recovery"
BINARY = Path(os.environ["LCSAS_RESTORE_BIN"]) if os.environ.get(
    "LCSAS_RESTORE_BIN"
) else RECOVERY_DIR / "build" / "lcsas-restore"

# The fixture builder lives under recovery/tests/.
sys.path.insert(0, str(RECOVERY_DIR / "tests"))


def _require_binary() -> None:
    _override = os.environ.get("LCSAS_RESTORE_BIN")
    if _override and not (BINARY.is_file() and os.access(BINARY, os.X_OK)):
        pytest.skip(f"LCSAS_RESTORE_BIN={_override!r} not executable")
    if not BINARY.exists():
        pytest.skip(
            f"{BINARY} not built; run `lcsas recovery build --arch host` first",
        )


def _build_repo_with_offloaded_packs(
    tmp: Path,
) -> tuple[Path, Path, Path, dict[str, bytes]]:
    """Build a real rustic repo, then move every pack out of the repo's
    ``data/`` into a separate ``mount`` directory.  The repo metadata
    (keys/index/snapshots) stays where the binary will read it via
    ``--repo``; the packs must come from ``--pack-search``, which is
    where the meta-disc exclusion logic runs.
    """
    import test_e2e  # type: ignore[import-not-found]

    repo = tmp / "repo"
    pwfile = tmp / "pw"
    pwfile.write_text("correct-horse-battery-staple\n")
    files = {
        "hello.txt": b"hello " * 256,
        "blob.bin": os.urandom(4096),
    }
    test_e2e.build_repo(
        repo, "correct-horse-battery-staple", files,
        v2=False,
    )

    mount = tmp / "mnt"
    (mount / "data").mkdir(parents=True)
    data_dir = repo / "data"
    for entry in sorted(data_dir.iterdir()):
        if entry.is_file():
            shutil.move(str(entry), str(mount / "data" / entry.name))
    return repo, pwfile, mount, files


def test_tier1_meta_disc_excluded_when_sentinel_present_then_included_when_removed(
    tmp_path: Path,
) -> None:
    """Single end-to-end check: with the meta sentinel present at the
    pack-search path, the binary treats it as the live meta disc and
    refuses to read packs from it (restore fails — no pack source).
    After the sentinel is removed (simulating the operator ejecting
    the meta disc and remounting a data disc at the same path), the
    same binary invocation succeeds.

    This is the smoke; the load-bearing assertion is the C unit test
    in ``recovery/tests/test_disc_locator.c`` which exercises the
    locator API directly.
    """
    _require_binary()
    tmp = Path(tempfile.mkdtemp(prefix="lcsas_meta_live_", dir=str(tmp_path)))
    try:
        repo, pwfile, mount, files = _build_repo_with_offloaded_packs(tmp)
        target = tmp / "out"
        target.mkdir()

        # The "mount point" holds the disc's data/ subtree.  Phase 1
        # adds the meta-sentinel so the locator treats it as live meta.
        sentinel = mount / "recovery" / "scripts" / "restore.sh"
        sentinel.parent.mkdir(parents=True, exist_ok=True)
        sentinel.write_text("#!/bin/sh\n")

        # Phase 1: sentinel is present -> meta-disc is "live" -> the
        # binary must NOT serve packs from the mount.  We run
        # non-interactive so the fail-fast path returns and we don't
        # block on a prompt.
        proc = subprocess.run(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(mount),
                "--meta-disc", str(mount),
                "--interactive", "off",
                "--verbose",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode != 0, (
            "expected non-zero exit when meta sentinel is present and the "
            "pack-search path equals the meta path; got 0.  Either the "
            "exclusion is broken or the sentinel probe never fired.\n"
            f"stderr:\n{proc.stderr}"
        )
        # The failure mode MUST be "pack not found" -- i.e., the locator
        # ran but every candidate was excluded.  If the binary fails for
        # any earlier reason (bad password, missing repo, etc.) the test
        # would pass even with the fix reverted; pin the reason here.
        assert "pack not found:" in proc.stderr, (
            "phase 1 failed for a non-locator reason (test is asserting "
            "on the wrong thing):\n"
            f"stderr:\n{proc.stderr}"
        )

        # Reset target (a partial restore may have written some files;
        # we want a clean run for phase 2).
        shutil.rmtree(str(target))
        target.mkdir()

        # Phase 2: remove the sentinel (simulating eject + remount as
        # a data disc).  The same invocation must succeed because the
        # mount is no longer recognised as the live meta disc.
        sentinel.unlink()
        sentinel.parent.rmdir()
        (mount / "recovery").rmdir()

        proc = subprocess.run(
            [
                str(BINARY),
                "--repo", str(repo),
                "--password-file", str(pwfile),
                "--target", str(target),
                "--snapshot", "latest",
                "--pack-search", str(mount),
                "--meta-disc", str(mount),
                "--interactive", "off",
                "--verbose",
            ],
            capture_output=True, text=True, timeout=60,
        )
        assert proc.returncode == 0, (
            f"expected restore to succeed after sentinel removed (issue "
            f"#143): rc={proc.returncode}\nstderr:\n{proc.stderr}"
        )

        # Verify content as well: this proves the locator actually
        # served packs from the (formerly-meta) mount.
        for name, content in files.items():
            got = (target / name).read_bytes()
            assert got == content, (
                f"{name} mismatch after restore via post-eject mount"
            )
    finally:
        shutil.rmtree(str(tmp), ignore_errors=True)
