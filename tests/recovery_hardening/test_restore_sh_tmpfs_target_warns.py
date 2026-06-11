"""Hardening tests: RAM-backed (tmpfs) restore-target warning [BOOT-05].

The documented default target is ``/tmp/restored`` and on many
mainstream distros — and nearly every live-USB session, the supported
no-OS route — ``/tmp`` is a RAM-backed tmpfs.  An heir who accepts the
default gets a "successful" restore that evaporates at poweroff.
These tests pin the guard restore.sh runs right after creating the
target directory:

* tmpfs/ramfs target + a TTY → boxed warning + "Continue restoring
  into RAM anyway? [y/N]" prompt; default (n) aborts before any tier
  runs;
* tmpfs target, non-interactive → warning on stderr, restore proceeds
  (automation that legitimately restores to tmp paths keeps working);
* ``LCSAS_FORBID_TMPFS_TARGET=1`` makes the non-interactive case fatal;
* disk-backed targets produce no warning and no prompt;
* the ``/proc/mounts`` awk fallback (no findmnt) detects tmpfs via the
  ``LCSAS_PROC_MOUNTS`` test seam.
"""

from __future__ import annotations

import os
import pty
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Default rust-triple the script picks on Linux x86_64 hosts.
HOST_TARGET = "x86_64-unknown-linux-musl"

WARNING_SNIPPET = "WILL BE LOST when this computer powers off"
PROMPT_SNIPPET = "Continue restoring into RAM anyway?"


def _make_repo_skeleton(root: Path, name: str) -> Path:
    """Make a minimal restic-format-shaped repo dir at root/<name>."""
    repo = root / name
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    (repo / "snapshots").mkdir()
    (repo / "keys" / "stub_key").write_text("stub")
    return repo


def _install_stub_binary(recovery: Path, target: str, name: str) -> Path:
    """Stub tier binary that prints its argv (``ARG: ...``) and exits 0."""
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


def _install_findmnt_stub(stub_dir: Path, fstype: str) -> None:
    """PATH-stub findmnt that reports ``fstype`` for every query."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "findmnt"
    stub.write_text(f"#!/bin/sh\necho {fstype}\n")
    stub.chmod(0o755)


def _stub_args(stdout: str) -> list[str]:
    return [
        line.removeprefix("ARG: ")
        for line in stdout.splitlines()
        if line.startswith("ARG: ")
    ]


def _recovery_fixture(tmp_path: Path) -> Path:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    return recovery


def _env(stub_dir: Path | None) -> dict[str, str]:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_PASSWORD": "stub-pw",
    }
    for var in (
        "LCSAS_PWFILE",
        "LCSAS_PROC_MOUNTS",
        "LCSAS_ALLOW_TMPFS_TARGET",
        "LCSAS_FORBID_TMPFS_TARGET",
    ):
        env.pop(var, None)
    if stub_dir is not None:
        env["PATH"] = f"{stub_dir}:{env['PATH']}"
    return env


# ── tmpfs target + TTY: warn, prompt, abort on default (n) ────────────


def test_tmpfs_target_warns_and_prompts(tmp_path: Path) -> None:
    """With a TTY on stdin, a tmpfs target prompts; 'n' aborts pre-tier."""
    recovery = _recovery_fixture(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")

    master, slave = pty.openpty()
    try:
        proc = subprocess.Popen(
            ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored")],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_env(stubs),
            text=True,
        )
        os.close(slave)
        os.write(master, b"n\n")
        out, err = proc.communicate(timeout=30)
    finally:
        os.close(master)

    assert WARNING_SNIPPET in err, (
        f"tmpfs target must produce the RAM warning.\nstderr:\n{err}"
    )
    assert PROMPT_SNIPPET in err, (
        f"a TTY-attached run must be prompted.\nstderr:\n{err}"
    )
    assert proc.returncode != 0, (
        f"answering 'n' must abort the restore; got rc=0.\n"
        f"stdout:\n{out}\nstderr:\n{err}"
    )
    assert "ARG: " not in out, (
        f"no tier may run after the operator declines.\nstdout:\n{out}"
    )


# ── tmpfs target, non-interactive: warn on stderr, continue ───────────


def test_tmpfs_target_noninteractive_warns_continues(tmp_path: Path) -> None:
    """Without a TTY the warning prints but the restore proceeds."""
    recovery = _recovery_fixture(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    target = tmp_path / "restored"

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env=_env(stubs), timeout=30,
        # Detach from any controlling terminal so the /dev/tty probe
        # fails deterministically even when pytest runs in a terminal.
        start_new_session=True,
    )
    assert WARNING_SNIPPET in res.stderr, (
        f"non-interactive tmpfs restore must still warn.\n"
        f"stderr:\n{res.stderr}"
    )
    assert PROMPT_SNIPPET not in res.stderr, (
        f"no prompt may fire without a TTY.\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert args, (
        f"the script must proceed past the check to tier 1.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert res.returncode == 0, (
        f"the check must not cause a non-interactive failure; "
        f"rc={res.returncode}.\nstderr:\n{res.stderr}"
    )


# ── LCSAS_FORBID_TMPFS_TARGET=1 makes non-interactive runs fail ───────


def test_forbid_env_blocks(tmp_path: Path) -> None:
    """LCSAS_FORBID_TMPFS_TARGET=1 + tmpfs target → warn and hard-fail."""
    recovery = _recovery_fixture(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    env = _env(stubs)
    env["LCSAS_FORBID_TMPFS_TARGET"] = "1"

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored")],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env=env, timeout=30,
        start_new_session=True,
    )
    assert res.returncode != 0, (
        f"LCSAS_FORBID_TMPFS_TARGET=1 must abort on a tmpfs target.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert WARNING_SNIPPET in res.stderr, (
        f"the abort must still show the RAM warning.\nstderr:\n{res.stderr}"
    )
    assert "LCSAS_FORBID_TMPFS_TARGET" in res.stderr, (
        f"the abort must name the env knob.\nstderr:\n{res.stderr}"
    )
    assert "ARG: " not in res.stdout, (
        f"no tier may run when the target is forbidden.\n"
        f"stdout:\n{res.stdout}"
    )


# ── disk-backed targets: no warning, no prompt ────────────────────────


def test_disk_target_silent(tmp_path: Path) -> None:
    """An ext4 target produces neither warning nor prompt."""
    recovery = _recovery_fixture(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "ext4")
    target = tmp_path / "restored"

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env=_env(stubs), timeout=30,
        start_new_session=True,
    )
    assert WARNING_SNIPPET not in res.stderr, (
        f"disk-backed targets must not warn.\nstderr:\n{res.stderr}"
    )
    assert PROMPT_SNIPPET not in res.stderr, (
        f"disk-backed targets must not prompt.\nstderr:\n{res.stderr}"
    )
    assert res.returncode == 0, (
        f"restore.sh failed on a disk target; rc={res.returncode}.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert _stub_args(res.stdout), "tier 1 must run normally"


# ── /proc/mounts awk fallback (no findmnt) via the test seam ──────────


def test_proc_mounts_fallback(tmp_path: Path) -> None:
    """LCSAS_PROC_MOUNTS forces the awk fallback; longest prefix wins."""
    recovery = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"
    # Canonical path: the awk matcher compares `pwd -P` output against
    # the mount-point column, so symlinked tmp bases must be resolved.
    real_base = Path(os.path.realpath(tmp_path))
    mounts = tmp_path / "proc_mounts"
    mounts.write_text(
        "/dev/root / ext4 rw 0 0\n"
        f"tmpfs {real_base} tmpfs rw 0 0\n"
    )
    env = _env(None)  # no findmnt stub: the seam bypasses findmnt
    env["LCSAS_PROC_MOUNTS"] = str(mounts)

    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target)],
        stdin=subprocess.DEVNULL,
        capture_output=True, text=True,
        env=env, timeout=30,
        start_new_session=True,
    )
    assert WARNING_SNIPPET in res.stderr, (
        f"the /proc/mounts fallback must detect the tmpfs target "
        f"(longest-prefix match over {mounts}).\nstderr:\n{res.stderr}"
    )
    assert res.returncode == 0, (
        f"non-interactive fallback detection must warn-and-continue; "
        f"rc={res.returncode}.\nstderr:\n{res.stderr}"
    )
    assert _stub_args(res.stdout), "tier 1 must still run"
