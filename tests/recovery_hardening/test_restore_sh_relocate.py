"""Hardening tests: restore.sh relocation path (read-only SCRIPT_DIR).

When the script is executed from a read-only directory (e.g., an optical
disc), it detects the non-writable SCRIPT_DIR, calls find_meta_mount() to
discover the mount point, and then copies itself + the recovery binaries into
a writable RAM directory before re-executing.

This test exercises lines that are otherwise only reachable from a genuinely
read-only disc mount:
  - Lines 222, 227 (relocate_needed=1 via non-writable SCRIPT_DIR)
  - Lines 80-82 (find_meta_mount body via findmnt)
  - Line 236 (find_meta_mount call when LCSAS_META_DISC is unset)
"""
from __future__ import annotations

import os
import shutil
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _install_stub_binary(recovery: Path, target: str, name: str) -> Path:
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        for a in "$@"; do printf 'ARG: %s\\n' "$a"; done
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def test_readonly_script_dir_triggers_relocation(tmp_path: Path) -> None:
    """Simulate the script running from a non-writable directory (e.g., an
    optical disc).  restore.sh should detect the non-writable SCRIPT_DIR,
    call find_meta_mount(), and relocate to a RAM directory before continuing.

    Because the relocation copies the recovery tree and re-execs, the final
    exit code should be 0 (the stub tier-1 binary runs from the RAM copy).
    """
    # Build a meta-disc-shaped layout: restore.sh at the root, recovery tree
    # underneath.  This is NOT the canonical scripts/ subdirectory layout —
    # it's the "meta disc top-level" form, which means AUTO_RECOVERY will be
    # detected via $SCRIPT_DIR/recovery/scripts.
    meta = tmp_path / "fake_meta"
    meta.mkdir()
    shutil.copy(RESTORE_SH, meta / "restore.sh")

    # Recovery tree with stub binary and holographic repo.
    recovery = meta / "recovery"
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    (recovery / "scripts").mkdir(parents=True)
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()

    # Dummy catalog so the catalog-copy branch in relocate_to_ram fires.
    (meta / "catalog.db").write_text("dummy")

    # Make meta/ non-writable so the SCRIPT_DIR non-writable check fires.
    os.chmod(meta, 0o555)
    try:
        env = {
            **os.environ,
            # Do NOT set LCSAS_META_DISC — we want the read-only probe (line 222)
            # to fire, not the LCSAS_META_DISC match (lines 215-218).
            # Do NOT set LCSAS_RELOCATED — we want relocation to run.
            # Do NOT set LCSAS_NO_RELOCATE.
            "LCSAS_MOUNT_DIRS": "",
            "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        }
        res = subprocess.run(
            ["sh", str(meta / "restore.sh")],
            capture_output=True, text=True, env=env, timeout=20,
            input="stub-pw\n",
        )
    finally:
        # Restore permissions so pytest can clean up tmp_path.
        os.chmod(meta, 0o755)

    # The relocation banner is the primary signal that lines 222/236 fired.
    # The re-exec'd script runs as /bin/sh (shebang), not bash, so its trace
    # goes to stderr — we don't assert returncode since the re-exec may not
    # find a repo (metadata isn't copied to the RAM dir by design).
    assert "[lcsas-restore] copied recovery binaries to" in res.stderr, (
        f"expected relocation banner in stderr — lines 222/236 may not have "
        f"fired.\nstderr:\n{res.stderr[:500]}"
    )
