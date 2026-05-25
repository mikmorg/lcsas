"""Issue #223 — restore.sh detects present-but-corrupted tier binaries.

Two failure modes are pinned:

  1. **Zero-byte file**.  POSIX `sh` "execs" an empty file as a script
     that exits 0 with no work done -- without a size check, a
     bit-rotted tier-1 binary silently claims success and the operator
     gets nothing restored.
  2. **Wrong-architecture binary** (e.g. Windows PE shipped to a Linux
     host, or an aarch64 ELF run on x86_64).  ``exec`` fails with
     rc=126 "Exec format error"; under default cascade semantics the
     shell exits and the cascade never reaches tier 2.

Both must be detected BEFORE dispatch and trigger a fall-through to
the next tier, with a clear diagnostic for the operator.  The
existing LCSAS_TIER_FALLBACK=1 hedge handles non-zero EXIT codes
from a tier-1 that loaded; it does NOT cover "binary couldn't be
loaded in the first place", which is what #223 closes.
"""
from __future__ import annotations

import os
import struct
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _make_repo(recovery: Path) -> None:
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    # Tier-2 skip (#227) bypasses rustic-static when $REPO/data/ is
    # absent (multi-disc archive — rustic can't drive disc swaps).
    # These fixtures pin tier-1-corrupt → tier-2-fallback semantics,
    # so the data/ dir must exist to keep tier 2 in the cascade.
    (repo / "data").mkdir()


def _install_zero_byte_binary(recovery: Path, name: str) -> Path:
    """A binary that exists, is +x, and is empty -- the bit-rot case."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.touch()
    stub.chmod(0o755)
    assert stub.stat().st_size == 0
    return stub


def _install_wrong_arch_binary(recovery: Path, name: str) -> Path:
    """A binary with a Windows PE header on Linux: exec returns 126.

    We don't need a real .exe -- the MZ + PE\\0\\0 header bytes are
    enough to make the Linux kernel binfmt loader reject it with
    ENOEXEC, which the shell reports as exit code 126.
    """
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    # Minimal "this is a PE/COFF executable" sniff: MZ header at byte 0,
    # PE signature pointer at byte 0x3c, "PE\0\0" at the indicated
    # offset.  Neither the binfmt_elf nor binfmt_script handler will
    # claim it; binfmt_misc may, but the test only needs exec to fail.
    data = bytearray(b"MZ" + b"\x00" * 0x3a)
    data += struct.pack("<I", 0x40)
    data += b"\x00" * (0x40 - len(data))
    data += b"PE\x00\x00"
    stub.write_bytes(bytes(data))
    stub.chmod(0o755)
    return stub


def _install_succeeding_binary(recovery: Path, name: str) -> Path:
    """Tier-2 / tier-N stub that prints a marker and exits 0."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "SUCCESS_{name}: stub ran"
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _run(recovery: Path, target_dir: Path,
         env_extra: dict[str, str] | None = None,
         ) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        # Stub binaries here -- no real data discs to discover.
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        **(env_extra or {}),
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )


# ── Zero-byte tier-1: must fall through under default semantics ─────


def test_zero_byte_tier1_falls_through_to_tier2(tmp_path: Path) -> None:
    """A zero-byte tier-1 must NOT silently succeed.  The cascade
    must skip tier 1 and reach a working tier 2.  This holds under
    DEFAULT semantics (no LCSAS_TIER_FALLBACK) -- the pre-flight
    check fires before the `exec` decision, so the bare-minimum
    recovery story is preserved while corruption is still caught."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_zero_byte_binary(recovery, "lcsas-restore")
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target)
    assert res.returncode == 0, (
        f"cascade should fall through zero-byte tier-1 to tier-2; "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" in res.stdout, (
        f"tier-2 stub did not run; stdout:\n{res.stdout}"
    )
    assert "pre-flight" in res.stderr, (
        f"missing pre-flight diagnostic for the operator; stderr:\n{res.stderr}"
    )


# ── Wrong-arch tier-1: must fall through under default semantics ────


def test_wrong_arch_tier1_falls_through_to_tier2(tmp_path: Path) -> None:
    """A wrong-arch tier-1 (Windows PE on Linux) must NOT abort the
    cascade.  Default cascade semantics call `exec` -- which on a
    wrong-arch binary returns 126 "Exec format error" and exits the
    shell.  The pre-flight detects this before the `exec` decision
    and the cascade reaches a working tier 2."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_wrong_arch_binary(recovery, "lcsas-restore")
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target)
    assert res.returncode == 0, (
        f"cascade should fall through wrong-arch tier-1 to tier-2; "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" in res.stdout, (
        f"tier-2 stub did not run; stdout:\n{res.stdout}"
    )
    assert "pre-flight" in res.stderr, (
        f"missing pre-flight diagnostic for the operator; stderr:\n{res.stderr}"
    )


# ── Symmetry for tier 2: zero-byte rustic-static must also skip ─────


def test_zero_byte_tier2_skipped(tmp_path: Path) -> None:
    """The same pre-flight applies to tier 2.  When tier 1 is absent
    and tier 2 is zero-byte, the cascade must fall through to tier 3
    (or hard-error) -- never silently exit 0."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    # No tier-1 at all -- exercise tier-2's pre-flight directly.
    _install_zero_byte_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    # No tier 3 stub here -- the cascade should reach the hard-error
    # path, NOT silently succeed.
    res = _run(recovery, target, env_extra={"LCSAS_ALLOW_PYTHON_TIER": "0"})
    assert res.returncode != 0, (
        f"zero-byte tier-2 must not silently succeed; "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "pre-flight" in res.stderr, (
        f"missing tier-2 pre-flight diagnostic; stderr:\n{res.stderr}"
    )
