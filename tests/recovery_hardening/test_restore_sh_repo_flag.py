"""Hardening test: --repo NAME flag on restore.sh (Issue #91).

Checks that restore.sh accepts ``--repo NAME`` as a CLI flag equivalent to
``LCSAS_REPO=NAME``, making multi-tenant repo selection discoverable from
``--help`` without requiring environment-variable knowledge.
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


def _stub_args(stdout: str) -> list[str]:
    """Extract the ARG: lines printed by the stub binary."""
    return [
        line.removeprefix("ARG: ")
        for line in stdout.splitlines()
        if line.startswith("ARG: ")
    ]


def _arg_value(args: list[str], flag: str) -> str | None:
    """Return the value of ``--flag X`` in the arg list, or None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


# ── Test 1: --repo flag appears in --help ────────────────────────────


def test_repo_flag_in_help() -> None:
    """``sh restore.sh --help`` output must contain ``--repo``."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, (
        f"--help exited {res.returncode}.\nstderr:\n{res.stderr}"
    )
    assert "--repo" in res.stdout, (
        f"--help output must document the --repo flag; got:\n{res.stdout}"
    )


# ── Test 2: --repo flag selects the named tenant ─────────────────────


def test_repo_flag_selects_tenant(tmp_path: Path) -> None:
    """``sh restore.sh --repo alpha RECOVERY TARGET`` should select alpha
    and pass its path as ``--repo DIR`` to the tier-1 stub binary."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    alpha = _make_repo_skeleton(recovery / "metadata", "alpha")
    _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    full_env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
    }
    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--repo", "alpha",
            str(recovery), str(target), "latest",
        ],
        input="stub-pw\n",
        capture_output=True, text=True,
        env=full_env, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh --repo alpha failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(alpha), (
        f"--repo alpha should pass {alpha!r} to the binary; got "
        f"{_arg_value(args, '--repo')!r}.  full argv: {args}"
    )


# ── Test 3: --repo flag overrides LCSAS_REPO env var ─────────────────


def test_repo_flag_overrides_env(tmp_path: Path) -> None:
    """``--repo bravo`` must win over ``LCSAS_REPO=alpha`` in the
    environment — the flag is parsed last and sets LCSAS_REPO
    unconditionally before the repo-discovery block runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    bravo = _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    full_env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_REPO": "alpha",  # env says alpha …
    }
    res = subprocess.run(
        [
            "sh", str(RESTORE_SH),
            "--repo", "bravo",  # … flag says bravo — flag must win
            str(recovery), str(target), "latest",
        ],
        input="stub-pw\n",
        capture_output=True, text=True,
        env=full_env, timeout=15,
    )
    assert res.returncode == 0, (
        f"restore.sh --repo bravo (with LCSAS_REPO=alpha) failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(bravo), (
        f"--repo bravo should override LCSAS_REPO=alpha; got "
        f"{_arg_value(args, '--repo')!r}.  full argv: {args}"
    )


# ── Test 4: --repo with no following argument exits non-zero ──────────


def test_repo_flag_missing_arg_exits_nonzero() -> None:
    """``sh restore.sh --repo`` with no NAME following must exit non-zero."""
    full_env = {
        **os.environ,
        "LCSAS_NO_RELOCATE": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--repo"],
        capture_output=True, text=True,
        env=full_env, timeout=10,
    )
    assert res.returncode != 0, (
        f"restore.sh --repo (no argument) should exit non-zero; got 0.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
