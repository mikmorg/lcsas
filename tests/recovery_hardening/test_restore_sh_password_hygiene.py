"""test_restore_sh_password_hygiene.py -- restore.sh password-file cleanup.

Issue #363 (H1): on the default (non-fallback) path the tiers used to
``exec`` the recovery binary, which replaced the shell process so the
``EXIT`` trap that shreds the temporary password file never fired -- the
operator's plaintext passphrase was left in ``/tmp/lcsas-pw.XXXXXX``
after every successful restore.  The fix runs the binary instead of
exec-ing it, so the trap fires and removes the temp file.

This pins that a completed restore via a stub tier leaves NO
``/tmp/lcsas-pw.*`` file behind, and that the tier still dispatches (the
run-not-exec change did not break the happy path).
"""

from __future__ import annotations

import glob
import os
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"
PW_GLOB = "/tmp/lcsas-pw.*"


def _repo_with_data(metadata_root: Path, name: str) -> Path:
    repo = metadata_root / name
    for sub in ("keys", "index", "data", "snapshots"):
        (repo / sub).mkdir(parents=True)
    (repo / "keys" / "stub_key").write_text("stub")
    (repo / "data" / "00").mkdir()
    (repo / "data" / "00" / ("0" * 64)).write_bytes(b"pack")
    return repo


def _install_restic_stub(recovery: Path, target: str = HOST_TARGET) -> Path:
    """A stub stock restic that succeeds (drives tier 2b)."""
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "restic"
    stub.write_text("#!/bin/sh\nexit 0\n")
    stub.chmod(0o755)
    return stub


def test_temp_password_file_is_shredded_after_restore(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_restic_stub(recovery)
    _repo_with_data(recovery / "metadata", "alpha")
    target = tmp_path / "restored"

    before = set(glob.glob(PW_GLOB))
    # Password supplied via env so no prompt; LCSAS_PWFILE deliberately
    # unset so restore.sh creates its own /tmp/lcsas-pw.XXXXXX temp file.
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_PASSWORD": "stub-password",
    }
    env.pop("LCSAS_PWFILE", None)
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=env, timeout=30,
    )
    assert res.returncode == 0, (
        f"restore did not complete via a stub tier.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    after = set(glob.glob(PW_GLOB))
    leaked = after - before
    assert not leaked, (
        "restore.sh left a plaintext password file behind after the "
        f"restore (exec leaked it; the EXIT trap must fire): {leaked}"
    )


def test_restore_sh_does_not_exec_the_tier_binary() -> None:
    """Guard the fix in source: the default-path tiers must run-then-exit
    (so the cleanup trap fires), never ``exec`` the recovery binary."""
    src = RESTORE_SH.read_text()
    assert 'exec "$RESTORE_BIN"' not in src, "tier 1 still exec-s (issue #363)"
    assert 'exec "$RUSTIC_BIN"' not in src, "tier 2 still exec-s (issue #363)"
    assert 'exec "$STDTOOL_BIN"' not in src, "tier 2b still exec-s (issue #363)"
    assert "exec env RESTIC_PASSWORD_FILE" not in src, (
        "tier 2b (restic form) still exec-s (issue #363)"
    )
