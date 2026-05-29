"""Shell-coverage tests for OS/arch detection branches in restore.sh.

Lines 383-409 of restore.sh contain a case statement that maps
(OS, MACHINE) to a rust-triple target.  On a Linux x86_64 host only
the first branch is ever exercised.  These tests inject a fake ``uname``
on the PATH so that every branch — including two error-exit paths — is
reachable without hardware or cross-compilation.

Adds coverage for:
  • Linux aarch64    → aarch64-unknown-linux-musl
  • Linux armv7l     → armv7-unknown-linux-gnueabihf
  • Darwin arm64     → aarch64-apple-darwin
  • Darwin x86_64   → x86_64-apple-darwin
  • Linux riscv64    → unsupported Linux machine (exit 1)
  • FreeBSD x86_64   → unsupported OS (exit 1)
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"


def _fake_uname_dir(tmp_path: Path, sysname: str, machine: str) -> Path:
    """Create a directory with a fake ``uname`` that returns the given values."""
    d = tmp_path / f"fake_uname_{sysname}_{machine}"
    d.mkdir()
    script = d / "uname"
    script.write_text(
        "#!/bin/sh\n"
        f'case "$1" in\n'
        f"  -s) echo '{sysname}' ;;\n"
        f"  -m) echo '{machine}' ;;\n"
        f"  *)  echo '{sysname}' ;;\n"
        "esac\n"
    )
    script.chmod(0o755)
    return d


def _make_recovery(tmp_path: Path) -> Path:
    """Create a minimal recovery fixture (holographic layout)."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()
    # bin/ must exist so arg-parsing recognises the first positional arg as RECOVERY.
    (recovery / "bin").mkdir()
    return recovery


def _install_target_stub(recovery: Path, target: str) -> None:
    """Install a stub binary for *target* that prints a marker on stdout."""
    bin_dir = recovery / "bin" / target
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "lcsas-restore"
    stub.write_text(
        "#!/bin/sh\n"
        "echo CORRECT_TARGET_BINARY_RAN\n"
        "exit 0\n"
    )
    stub.chmod(0o755)


def _run(
    recovery: Path,
    target_dir: Path,
    uname_dir: Path,
) -> subprocess.CompletedProcess[str]:
    env: dict[str, str] = {
        **os.environ,
        "LCSAS_RELOCATED": "/fake/meta",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_MOUNT_DIRS": "",
        "PATH": str(uname_dir) + ":" + os.environ.get("PATH", ""),
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input="testpassword\n",
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )


@pytest.mark.parametrize(
    "sysname,machine,expected_target",
    [
        ("Linux",           "aarch64", "aarch64-unknown-linux-musl"),
        ("Linux",           "armv7l",  "armv7-unknown-linux-gnueabihf"),
        ("Darwin",          "arm64",   "aarch64-apple-darwin"),
        ("Darwin",          "x86_64",  "x86_64-apple-darwin"),
        ("MINGW64_NT-10.0", "x86_64",  "x86_64-pc-windows-gnu"),
    ],
)
def test_target_selected_for_platform(
    tmp_path: Path,
    sysname: str,
    machine: str,
    expected_target: str,
) -> None:
    """restore.sh selects the correct rust-triple for each supported platform."""
    recovery = _make_recovery(tmp_path)
    _install_target_stub(recovery, expected_target)
    uname_dir = _fake_uname_dir(tmp_path, sysname, machine)
    result = _run(recovery, tmp_path / "restored", uname_dir)

    assert result.returncode == 0, (
        f"expected exit 0 for {sysname}/{machine} → {expected_target}; "
        f"got rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "CORRECT_TARGET_BINARY_RAN" in result.stdout, (
        f"stub for {expected_target} did not run.\n"
        f"stdout:\n{result.stdout}\n"
        f"stderr:\n{result.stderr}"
    )


def test_unsupported_linux_machine_exits_1(tmp_path: Path) -> None:
    """restore.sh exits 1 with a helpful message for an unknown Linux arch."""
    recovery = _make_recovery(tmp_path)
    uname_dir = _fake_uname_dir(tmp_path, "Linux", "riscv64")
    result = _run(recovery, tmp_path / "restored", uname_dir)

    assert result.returncode == 1, (
        f"expected exit 1 for Linux/riscv64; got rc={result.returncode}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "unsupported Linux machine" in result.stderr, (
        f"expected 'unsupported Linux machine' in stderr; got:\n{result.stderr}"
    )


def test_unsupported_macos_machine_exits_1(tmp_path: Path) -> None:
    """restore.sh exits 1 with a helpful message for an unknown macOS arch."""
    recovery = _make_recovery(tmp_path)
    uname_dir = _fake_uname_dir(tmp_path, "Darwin", "ppc64")
    result = _run(recovery, tmp_path / "restored", uname_dir)

    assert result.returncode == 1, (
        f"expected exit 1 for Darwin/ppc64; got rc={result.returncode}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "unsupported macOS machine" in result.stderr, (
        f"expected 'unsupported macOS machine' in stderr; got:\n{result.stderr}"
    )


def test_unsupported_windows_machine_exits_1(tmp_path: Path) -> None:
    """restore.sh exits 1 for an unsupported Windows arch."""
    recovery = _make_recovery(tmp_path)
    uname_dir = _fake_uname_dir(tmp_path, "MINGW64_NT-10.0", "aarch64")
    result = _run(recovery, tmp_path / "restored", uname_dir)

    assert result.returncode == 1, (
        f"expected exit 1 for MINGW64/aarch64; got rc={result.returncode}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "unsupported Windows machine" in result.stderr, (
        f"expected 'unsupported Windows machine' in stderr; got:\n{result.stderr}"
    )


def test_lcsas_target_overrides_uname(tmp_path: Path) -> None:
    """LCSAS_TARGET env bypasses uname-based detection entirely."""
    recovery = _make_recovery(tmp_path)
    target = "aarch64-unknown-linux-musl"
    _install_target_stub(recovery, target)
    # Fake uname reports x86_64 Linux — without the override the script
    # would pick x86_64-unknown-linux-musl and fail to find the binary.
    uname_dir = _fake_uname_dir(tmp_path, "Linux", "x86_64")
    env: dict[str, str] = {
        **os.environ,
        "LCSAS_RELOCATED": "/fake/meta",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TARGET": target,
        "PATH": str(uname_dir) + ":" + os.environ.get("PATH", ""),
    }
    result = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(tmp_path / "restored"), "latest"],
        input="testpassword\n",
        capture_output=True,
        text=True,
        timeout=15,
        env=env,
    )
    assert result.returncode == 0, (
        f"expected exit 0 with LCSAS_TARGET override; rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CORRECT_TARGET_BINARY_RAN" in result.stdout


def test_detect_arch_sh_used_when_present(tmp_path: Path) -> None:
    """When $RECOVERY/scripts/detect_arch.sh is executable, restore.sh uses
    it to determine MACHINE instead of calling uname directly (line 380)."""
    recovery = _make_recovery(tmp_path)
    target_triple = "x86_64-unknown-linux-musl"
    _install_target_stub(recovery, target_triple)

    # Create the detect_arch.sh in the recovery/scripts dir.
    scripts_dir = recovery / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    detect_arch = scripts_dir / "detect_arch.sh"
    detect_arch.write_text("#!/bin/sh\necho 'x86_64'\n")
    detect_arch.chmod(0o755)

    uname_dir = _fake_uname_dir(tmp_path, "Linux", "aarch64")  # uname would say aarch64
    result = _run(recovery, tmp_path / "restored", uname_dir)

    # detect_arch.sh says x86_64, so the x86_64 stub should run even though
    # uname would return aarch64.
    assert result.returncode == 0, (
        f"expected exit 0 when detect_arch.sh present; rc={result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert "CORRECT_TARGET_BINARY_RAN" in result.stdout, (
        f"x86_64 stub should run when detect_arch.sh returns x86_64; "
        f"stdout:\n{result.stdout}"
    )


def test_unsupported_os_exits_1(tmp_path: Path) -> None:
    """restore.sh exits 1 with a helpful message for an unrecognised OS."""
    recovery = _make_recovery(tmp_path)
    uname_dir = _fake_uname_dir(tmp_path, "FreeBSD", "x86_64")
    result = _run(recovery, tmp_path / "restored", uname_dir)

    assert result.returncode == 1, (
        f"expected exit 1 for FreeBSD/x86_64; got rc={result.returncode}\n"
        f"stderr:\n{result.stderr}"
    )
    assert "unsupported OS" in result.stderr, (
        f"expected 'unsupported OS' in stderr; got:\n{result.stderr}"
    )
