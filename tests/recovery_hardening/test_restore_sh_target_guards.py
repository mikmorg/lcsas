"""Hardening tests: restore.sh target-safety guards [UX-07] + [BOOT-05].

These exercise the two operator-protection gates that fire right before
the password prompt, focusing on the branches the rest of the
``test_restore_*.py`` suite does not reach so ``make shell-coverage``
sees them:

* UX-07 non-empty-target overwrite guard (restore.sh ~1003-1030):
  the boxed WARNING, the non-interactive refusal (exit 65 naming the
  override env var), the ``LCSAS_FORCE_NONEMPTY_TARGET=1`` bypass, and
  the interactive ``Type YES`` / not-YES branches via a pty.

* BOOT-05 RAM-backed (tmpfs) target guard (restore.sh ~1100-1127):
  the ``LCSAS_ALLOW_TMPFS_TARGET=1`` continue line, the TTY ``[y/N]``
  prompt (continue on ``y``; abort on anything else), and the
  ``/dev/tty`` fallback when stdin is redirected but a terminal exists.

Harness note: the gate drives coverage by rewriting ``sh restore.sh``
``subprocess.run`` calls to ``bash`` + ``LCSAS_SHELL_TRACE`` (see
``conftest.py``).  That wrapper only touches ``subprocess.run``, so the
interactive cases here -- which need a pty and therefore ``Popen`` --
replicate the same rewrite locally (``_run_pty``) so their TTY-only
lines also contribute to the trace.  Standalone (no trace env) they run
under ``sh`` unchanged.
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
NONEMPTY_WARNING = "do not look like a previous"
TMPFS_WARNING = "WILL BE LOST when this computer powers off"
TMPFS_PROMPT = "Continue restoring into RAM anyway?"
ALLOW_CONTINUE = "continuing into RAM"
EXIT_USER_ABORT = 65


# ── fixtures ──────────────────────────────────────────────────────────


def _make_recovery(tmp_path: Path) -> Path:
    """Writable recovery tree with a stub tier-1 binary and a
    discoverable holographic repo so the script reaches the guards
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


def _install_findmnt_stub(stub_dir: Path, fstype: str) -> None:
    """PATH-stub findmnt that reports ``fstype`` for every query."""
    stub_dir.mkdir(parents=True, exist_ok=True)
    stub = stub_dir / "findmnt"
    stub.write_text(f"#!/bin/sh\necho {fstype}\n")
    stub.chmod(0o755)


def _base_env() -> dict[str, str]:
    env = {
        **os.environ,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_SKIP_SPACE_CHECK": "1",
        "LCSAS_PASSWORD": "stub-pw",
        "LCSAS_REPO": "alpha",
    }
    for var in (
        "LCSAS_PWFILE",
        "LCSAS_PROC_MOUNTS",
        "LCSAS_ALLOW_TMPFS_TARGET",
        "LCSAS_FORBID_TMPFS_TARGET",
        "LCSAS_FORCE_NONEMPTY_TARGET",
    ):
        env.pop(var, None)
    return env


def _run_pipe(recovery: Path, target: Path, env: dict[str, str],
              stdin_text: str = "") -> subprocess.CompletedProcess[str]:
    """Run with stdin as a pipe (NOT a tty).  Goes through
    ``subprocess.run`` so the gate's trace wrapper applies."""
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=env, timeout=30,
        input=stdin_text,
    )


def _run_pty(recovery: Path, target: Path, env: dict[str, str],
             answer: str, *, redirect_stdin: bool = False) -> tuple[int, str]:
    """Run with a controlling pty so ``[ -t 0 ]`` (or the ``/dev/tty``
    fallback) is satisfied.  Returns (exit_status, combined_output).

    ``redirect_stdin=True`` keeps stdin as a pipe while still giving the
    child a controlling terminal, exercising the ``/dev/tty`` fallback
    branch (stdin redirected, but a human is still reachable).

    Mirrors ``conftest``'s rewrite so the TTY-only lines feed the gate's
    trace: when ``LCSAS_TRACE_VIA_BASH`` is set we invoke ``bash`` with
    ``LCSAS_SHELL_TRACE`` pointed at the shared trace file.
    """
    import pty

    interp = "sh"
    env = dict(env)
    if os.environ.get("LCSAS_TRACE_VIA_BASH") and os.environ.get(
        "LCSAS_SHELL_TRACE"
    ):
        interp = "bash"
        env["LCSAS_SHELL_TRACE"] = os.environ["LCSAS_SHELL_TRACE"]

    import fcntl
    import termios

    master, slave = pty.openpty()
    stdin_master = None
    if redirect_stdin:
        # Give the child a *controlling* tty (new session + TIOCSCTTY on
        # the slave) while feeding stdin from a separate pipe, so
        # `[ -t 0 ]` is false but the `/dev/tty` probe succeeds -- the
        # fallback branch.
        stdin_r, stdin_master = os.pipe()

        def _make_ctty() -> None:  # runs in the child before exec
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)

        proc = subprocess.Popen(
            [interp, str(RESTORE_SH), str(recovery), str(target), "latest"],
            stdin=stdin_r, stdout=slave, stderr=slave, env=env,
            close_fds=True, preexec_fn=_make_ctty,
        )
        os.close(stdin_r)
    else:
        proc = subprocess.Popen(
            [interp, str(RESTORE_SH), str(recovery), str(target), "latest"],
            stdin=slave, stdout=slave, stderr=slave, env=env,
            close_fds=True,
        )
    os.close(slave)
    # The interactive prompt reads from the controlling terminal in the
    # /dev/tty case, and from stdin otherwise; write the answer to both
    # channels so either read() succeeds.
    os.write(master, answer.encode())
    if stdin_master is not None:
        os.write(stdin_master, answer.encode())
        os.close(stdin_master)
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
    return rc, out.decode(errors="replace")


# ── UX-07 non-empty-target overwrite guard ───────────────────────────


def test_nonempty_foreign_target_noninteractive_refuses(tmp_path: Path) -> None:
    """A pre-populated foreign target with no TTY and no override is
    refused with exit 65 -- the warning prints and the env knob that
    unblocks automation is named (restore.sh 1007-1016, 1026-1028)."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "tax_return_2049.pdf").write_text("LIVE DATA")

    res = _run_pipe(recovery, target, _base_env())

    assert res.returncode == EXIT_USER_ABORT, (
        f"expected exit {EXIT_USER_ABORT}; got {res.returncode}\n"
        f"{res.stderr[:600]}"
    )
    combined = res.stdout + res.stderr
    assert NONEMPTY_WARNING in combined, f"warning not shown:\n{combined[:600]}"
    assert "LCSAS_FORCE_NONEMPTY_TARGET=1" in combined, (
        "non-TTY refusal must name the override env var"
    )
    assert "STUB-TIER1-RAN" not in combined, "tier 1 ran despite refusal"
    assert not (target / MARKER_NAME).exists(), "marker written despite refusal"
    assert (target / "tax_return_2049.pdf").read_text() == "LIVE DATA"


def test_nonempty_force_env_bypasses_guard(tmp_path: Path) -> None:
    """LCSAS_FORCE_NONEMPTY_TARGET=1 proceeds past the guard without a
    prompt on a foreign non-empty target (restore.sh 1005 short-circuit)."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "foreign.txt").write_text("x")
    env = {**_base_env(), "LCSAS_FORCE_NONEMPTY_TARGET": "1"}

    res = _run_pipe(recovery, target, env)

    combined = res.stdout + res.stderr
    assert NONEMPTY_WARNING not in combined, "warned despite force override"
    assert "STUB-TIER1-RAN" in combined, (
        f"tier 1 did not run under force override\n{combined[:600]}"
    )


def test_nonempty_foreign_target_interactive_yes_proceeds(
    tmp_path: Path,
) -> None:
    """Interactive 'YES' confirms the overwrite: the run proceeds past
    the guard and drops the marker (restore.sh 1018-1021)."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "foreign.txt").write_text("x")

    _rc, out = _run_pty(recovery, target, _base_env(), "YES\n")

    assert NONEMPTY_WARNING in out, f"warning not shown:\n{out[:600]}"
    assert "STUB-TIER1-RAN" in out, f"tier 1 did not run after YES:\n{out[:600]}"
    assert (target / MARKER_NAME).exists(), "marker not written after confirm"


def test_nonempty_foreign_target_interactive_not_yes_aborts(
    tmp_path: Path,
) -> None:
    """Interactive answer other than 'YES' aborts with exit 65 before any
    tier runs and leaves the live data untouched (restore.sh 1018-1023)."""
    recovery = _make_recovery(tmp_path)
    target = tmp_path / "myfiles"
    target.mkdir()
    (target / "tax_return_2049.pdf").write_text("LIVE DATA")

    rc, out = _run_pty(recovery, target, _base_env(), "no\n")

    assert rc == EXIT_USER_ABORT, f"expected exit {EXIT_USER_ABORT}; rc={rc}\n{out[:600]}"
    assert NONEMPTY_WARNING in out, f"warning not shown:\n{out[:600]}"
    assert "STUB-TIER1-RAN" not in out, "tier 1 ran despite abort"
    assert not (target / MARKER_NAME).exists(), "marker written despite abort"
    assert (target / "tax_return_2049.pdf").read_text() == "LIVE DATA"


# ── BOOT-05 RAM-backed (tmpfs) target guard ───────────────────────────


def test_tmpfs_allow_env_continues(tmp_path: Path) -> None:
    """LCSAS_ALLOW_TMPFS_TARGET=1 warns then continues into RAM without a
    prompt -- the explicit-opt-in line (restore.sh 1106)."""
    recovery = _make_recovery(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    env = {**_base_env(), "LCSAS_ALLOW_TMPFS_TARGET": "1"}
    env["PATH"] = f"{stubs}:{env['PATH']}"

    res = _run_pipe(recovery, tmp_path / "restored", env)

    combined = res.stdout + res.stderr
    assert TMPFS_WARNING in combined, f"RAM warning not shown:\n{combined[:600]}"
    assert ALLOW_CONTINUE in combined, (
        f"the explicit-opt-in continue line must print:\n{combined[:600]}"
    )
    assert TMPFS_PROMPT not in combined, "must not prompt when allow env set"
    assert "STUB-TIER1-RAN" in combined, (
        f"tier 1 must run under the allow override\n{combined[:600]}"
    )
    assert res.returncode == 0, f"rc={res.returncode}\n{combined[:600]}"


def test_tmpfs_interactive_yes_continues(tmp_path: Path) -> None:
    """A tmpfs target with a TTY prompts; 'y' continues into RAM
    (restore.sh 1108-1109, 1122)."""
    recovery = _make_recovery(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    env = _base_env()
    env["PATH"] = f"{stubs}:{env['PATH']}"

    _rc, out = _run_pty(recovery, tmp_path / "restored", env, "y\n")

    assert TMPFS_WARNING in out, f"RAM warning not shown:\n{out[:600]}"
    assert TMPFS_PROMPT in out, f"a TTY run must be prompted:\n{out[:600]}"
    assert "STUB-TIER1-RAN" in out, (
        f"tier 1 must run after 'y' at the RAM prompt:\n{out[:600]}"
    )


def test_tmpfs_interactive_no_aborts(tmp_path: Path) -> None:
    """A tmpfs target with a TTY prompts; the default ('n') aborts before
    any tier runs (restore.sh 1108-1109, 1124-1125)."""
    recovery = _make_recovery(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    env = _base_env()
    env["PATH"] = f"{stubs}:{env['PATH']}"

    rc, out = _run_pty(recovery, tmp_path / "restored", env, "n\n")

    assert TMPFS_WARNING in out, f"RAM warning not shown:\n{out[:600]}"
    assert TMPFS_PROMPT in out, f"a TTY run must be prompted:\n{out[:600]}"
    assert rc != 0, f"answering 'n' must abort; rc=0\n{out[:600]}"
    assert "STUB-TIER1-RAN" not in out, "tier 1 ran despite declining RAM target"


def test_tmpfs_dev_tty_fallback_continues(tmp_path: Path) -> None:
    """stdin redirected but a controlling terminal present: the script
    prompts on /dev/tty and 'y' continues (restore.sh 1113-1114)."""
    recovery = _make_recovery(tmp_path)
    stubs = tmp_path / "stubs"
    _install_findmnt_stub(stubs, "tmpfs")
    env = _base_env()
    env["PATH"] = f"{stubs}:{env['PATH']}"

    _rc, out = _run_pty(
        recovery, tmp_path / "restored", env, "y\n", redirect_stdin=True
    )

    assert TMPFS_WARNING in out, f"RAM warning not shown:\n{out[:600]}"
    assert TMPFS_PROMPT in out, (
        f"the /dev/tty fallback must still prompt:\n{out[:600]}"
    )
    assert "STUB-TIER1-RAN" in out, (
        f"tier 1 must run after 'y' on the /dev/tty fallback:\n{out[:600]}"
    )
