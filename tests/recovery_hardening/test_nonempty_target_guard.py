"""Hardening tests: restore.sh non-empty-target guard [UX-07].

Writing into a folder that already holds the heir's own files silently
overwrites live data with decades-old archive content.  restore.sh now
WARNS before the password prompt and refuses (exit 65) to write into a
non-empty target that does not look like a previous LCSAS restore --
identified by a hidden ``.lcsas-restore-marker`` file.  The supported
idempotent-resume re-run (RECOVER.txt RETRY SAFETY) must stay silent.

These tests drive recovery/scripts/restore.sh directly (pattern of
test_restore_sh_relocate.py).  A pty is used for the interactive cases
so the script's ``[ -t 0 ]`` TTY branch is exercised; a plain pipe
exercises the non-TTY refusal branch.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"
MARKER_NAME = ".lcsas-restore-marker"
WARNING_SNIPPET = "do not look like a previous"
EXIT_USER_ABORT = 65


def _make_recovery(tmp_path: Path) -> Path:
    """Build a writable recovery tree with a stub tier-1 binary and a
    discoverable holographic repo, so the script reaches the guard
    without relocating or tripping the no-recovery-binary check."""
    recovery = tmp_path / "recovery"
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True)
    stub = bin_dir / "lcsas-restore"
    stub.write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "STUB-TIER1-RAN"
        exit 0
    """))
    stub.chmod(0o755)
    (recovery / "scripts").mkdir()
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    return recovery


def _base_env() -> dict[str, str]:
    return {
        **os.environ,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_PASSWORD": "stub-pw",
        "LCSAS_REPO": "alpha",
    }


def _run_pipe(recovery: Path, target: Path, env: dict[str, str],
              stdin_text: str) -> subprocess.CompletedProcess[str]:
    """Run with stdin as a pipe (NOT a tty)."""
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=env, timeout=30,
        input=stdin_text,
    )


def _run_pty(recovery: Path, target: Path, env: dict[str, str],
             answer: str) -> tuple[int, str]:
    """Run with stdin attached to a pty so ``[ -t 0 ]`` is true.

    Returns (exit_status, combined_output).
    """
    import pty

    master, slave = pty.openpty()
    proc = subprocess.Popen(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        stdin=slave, stdout=slave, stderr=slave, env=env,
        close_fds=True,
    )
    os.close(slave)
    os.write(master, answer.encode())
    out = bytearray()
    try:
        while True:
            try:
                chunk = os.read(master, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
    finally:
        os.close(master)
    rc = proc.wait(timeout=30)
    # Popen.returncode is the decoded exit code (negative if signalled),
    # not a raw wait-status -- compare it directly.
    return rc, out.decode(errors="replace")


def test_foreign_nonempty_target_aborts_via_tty(tmp_path: Path) -> None:
    """A pre-populated foreign target + interactive 'no' aborts with
    exit 65 BEFORE any tier runs, and prints the warning."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "tax_return_2049.pdf").write_text("LIVE DATA")

    rc, out = _run_pty(recovery, target, _base_env(), "no\n")

    assert rc == EXIT_USER_ABORT, (
        f"expected exit {EXIT_USER_ABORT}; rc={rc}\n{out[:600]}"
    )
    assert WARNING_SNIPPET in out, f"warning not shown:\n{out[:600]}"
    assert "STUB-TIER1-RAN" not in out, "tier 1 ran despite abort"
    assert not (target / MARKER_NAME).exists(), "marker written despite abort"
    assert (target / "tax_return_2049.pdf").read_text() == "LIVE DATA"


def test_foreign_nonempty_target_proceeds_on_yes(tmp_path: Path) -> None:
    """Interactive 'YES' confirms: the run proceeds and writes the
    marker so future re-runs are silent."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "foreign.txt").write_text("x")

    _rc, out = _run_pty(recovery, target, _base_env(), "YES\n")

    assert "STUB-TIER1-RAN" in out, f"tier 1 did not run after YES:\n{out[:600]}"
    assert (target / MARKER_NAME).exists(), "marker not written after confirm"


def test_marker_bearing_target_no_prompt(tmp_path: Path) -> None:
    """A target carrying the marker (prior LCSAS restore) proceeds with
    no prompt -- the idempotent-resume case."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "restored"
    target.mkdir()
    (target / MARKER_NAME).write_text("")
    (target / "already_restored.txt").write_text("from a prior run")

    res = _run_pipe(recovery, target, _base_env(), "")

    assert WARNING_SNIPPET not in res.stdout + res.stderr, (
        "prompted despite marker present"
    )
    assert "STUB-TIER1-RAN" in res.stdout + res.stderr, (
        f"tier 1 did not run on a marker-bearing target\n{res.stderr[:600]}"
    )


def test_empty_target_no_prompt(tmp_path: Path) -> None:
    """An empty (or not-yet-existing) target proceeds silently."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "fresh"  # does not exist

    res = _run_pipe(recovery, target, _base_env(), "")

    combined = res.stdout + res.stderr
    assert WARNING_SNIPPET not in combined, "prompted on an empty target"
    assert "STUB-TIER1-RAN" in combined, f"tier 1 did not run\n{combined[:600]}"
    assert (target / MARKER_NAME).exists(), "marker not written"


def test_force_env_skips_prompt(tmp_path: Path) -> None:
    """LCSAS_FORCE_NONEMPTY_TARGET=1 skips the prompt on a foreign
    non-empty target."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "foreign.txt").write_text("x")
    env = {**_base_env(), "LCSAS_FORCE_NONEMPTY_TARGET": "1"}

    res = _run_pipe(recovery, target, env, "")

    combined = res.stdout + res.stderr
    assert WARNING_SNIPPET not in combined, "prompted despite force override"
    assert "STUB-TIER1-RAN" in combined, f"tier 1 did not run\n{combined[:600]}"


def test_non_tty_without_override_aborts_with_hint(tmp_path: Path) -> None:
    """No terminal + no override → refuse, naming the env var that lets
    automation proceed."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "foreign.txt").write_text("x")

    res = _run_pipe(recovery, target, _base_env(), "")

    assert res.returncode == EXIT_USER_ABORT, (
        f"expected exit {EXIT_USER_ABORT}; got {res.returncode}\n{res.stderr[:600]}"
    )
    combined = res.stdout + res.stderr
    assert WARNING_SNIPPET in combined
    assert "LCSAS_FORCE_NONEMPTY_TARGET=1" in combined, (
        "non-TTY refusal must name the override env var"
    )
    assert "STUB-TIER1-RAN" not in combined, "tier 1 ran despite refusal"
