"""Hardening test: --version flag on restore.sh (Issue #96).

Catches: restore.sh dropping the --version flag, or the flag silently
passing through to the positional-arg parser instead of printing and exiting.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Default rust-triple the C build chooses on Linux x86_64; matches
# what detect_arch.sh emits.  Tests run on the host arch so this is
# the only target we need a stub for.
HOST_TARGET = "x86_64-unknown-linux-musl"


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
    """Install a stub recovery binary at ``recovery/bin/<target>/<name>``.

    The stub just prints its argv to stdout (one arg per line, prefixed
    ``ARG: ``) so tests can assert the script invoked it with the
    expected flags.  It exits 0.
    """
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


# ── Test 1: --version flag exits zero ───────────────────────────────


def test_version_flag_exits_zero(tmp_path: Path) -> None:
    """``sh restore.sh --version`` must exit 0."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")

    full_env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--version"],
        capture_output=True, text=True,
        env=full_env, timeout=10,
    )
    assert res.returncode == 0, (
        f"--version should exit 0; got {res.returncode}.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )


# ── Test 2: --version flag output format ────────────────────────────


def test_version_flag_output_format(tmp_path: Path) -> None:
    """``sh restore.sh --version`` stdout must contain ``lcsas-restore.sh``.

    The placeholder ``@@BUILD_SHA@@`` being present is also acceptable —
    the test is about the output format, not the stamp value.
    """
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")

    full_env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--version"],
        capture_output=True, text=True,
        env=full_env, timeout=10,
    )
    assert res.returncode == 0, (
        f"--version should exit 0; got {res.returncode}.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "lcsas-restore.sh" in res.stdout, (
        f"--version output must contain 'lcsas-restore.sh'; got:\n{res.stdout}"
    )


# ── Test 3: --version appears in --help ─────────────────────────────


def test_version_in_help() -> None:
    """``sh restore.sh --help`` output must contain ``--version``."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, (
        f"--help exited {res.returncode}.\nstderr:\n{res.stderr}"
    )
    assert "--version" in res.stdout, (
        f"--help output must document the --version flag; got:\n{res.stdout}"
    )
