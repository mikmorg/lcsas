"""Hardening test (issue #227): restore.sh skips tier 2 when the
archive is multi-disc.

rustic-static (tier 2) expects all packs to live in a single local
``$REPO/data/`` directory.  LCSAS's holographic multi-disc layout
spreads packs across N data discs that need to be swapped through
one drive.  When the meta-disc-resident repo carries no local
``data/`` subtree, rustic cannot drive the swap and exits with a
cryptic "pack not found" error AFTER the meta disc is unmounted --
leaving the operator stranded.

The fix: detect the missing ``$REPO/data/`` before tier-2 dispatch
and fall straight through to tier 3 (standalone_restorer.py), which
DOES understand the LCSAS pack-search protocol.  Single-disc
archives (where ``$REPO/data/`` IS present) still go through tier 2
as before.

This test pins both halves of the behavior:
  * multi-disc layout (no $REPO/data/) -> tier 2 skipped, tier 3 runs;
  * single-disc layout (with $REPO/data/) -> tier 2 runs as before.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"


def _install_failing_binary(recovery: Path, name: str,
                            exit_code: int = 17) -> None:
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "FAKE {name}: simulated crash" >&2
        exit {exit_code}
    """))
    stub.chmod(0o755)


def _install_succeeding_binary(recovery: Path, name: str) -> None:
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "SUCCESS_{name}: stub ran"
        exit 0
    """))
    stub.chmod(0o755)


def _make_multidisc_repo(recovery: Path) -> Path:
    """Holographic multi-disc layout: repo carries keys/index/snapshots
    but NO data/ -- packs are on swappable data discs."""
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def _make_singledisc_repo(recovery: Path) -> Path:
    """Single-disc / legacy layout: repo carries its own data/."""
    repo = _make_multidisc_repo(recovery)
    (repo / "data").mkdir()
    return repo


def _install_tier3(recovery: Path, tmp_path: Path) -> Path:
    """Wire up a stub tier-3 (standalone_restorer.py + python3) that
    just prints a marker and exits 0.  Returns the stub bin dir
    (caller prepends to PATH)."""
    (recovery.parent / "standalone_restorer.py").write_text(
        "# stub tier-3 restorer\n"
    )
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    (stub_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN"
        exit 0
    """))
    (stub_dir / "python3").chmod(0o755)
    return stub_dir


def test_tier2_skipped_when_repo_has_no_local_data(tmp_path: Path) -> None:
    """Multi-disc fixture + LCSAS_TIER_FALLBACK=1: tier 1 crashes,
    tier 2 is present but must be SKIPPED (because $REPO/data/ is
    absent), and the script must reach tier 3."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_multidisc_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    # tier 2 binary is present BUT must not run for a multi-disc archive.
    # The stub would echo SUCCESS_rustic-static if invoked; absence of
    # that marker in stdout proves the skip.
    _install_succeeding_binary(recovery, "rustic-static")
    stub_path = _install_tier3(recovery, tmp_path)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "PATH": f"{stub_path}:" + os.environ.get("PATH", ""),
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"expected tier-3 success; rc={res.returncode}\n"
        f"stdout:{res.stdout}\nstderr:{res.stderr}"
    )
    # Tier 2 stub must NOT have been invoked.
    assert "SUCCESS_rustic-static" not in res.stdout, (
        "tier 2 ran on a multi-disc fixture -- the skip is not engaging.\n"
        f"stdout:\n{res.stdout}"
    )
    # Tier 3 stub MUST have been invoked.
    assert "TIER3_PYTHON_RAN" in res.stdout, (
        f"tier 3 did not run; stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    # Diagnostic banner explains the skip.
    assert "skipped" in res.stderr and "multi-disc" in res.stderr, (
        f"expected '[tier 2] skipped: ... multi-disc' diagnostic; "
        f"stderr:\n{res.stderr}"
    )


def test_tier2_runs_when_repo_has_local_data(tmp_path: Path) -> None:
    """Single-disc fixture: $REPO/data/ exists, so tier 2 MUST run
    (and succeed) under LCSAS_TIER_FALLBACK=1 after tier 1 crashes.
    This pins the bypass to multi-disc layouts only."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_singledisc_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_succeeding_binary(recovery, "rustic-static")

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n", capture_output=True, text=True,
        env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"single-disc fixture should succeed via tier 2; "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" in res.stdout, (
        f"tier 2 should have run on single-disc; stdout:\n{res.stdout}"
    )
    # No skip banner in this path.
    assert "skipped" not in res.stderr or "multi-disc" not in res.stderr, (
        f"unexpected tier-2 skip on single-disc fixture; "
        f"stderr:\n{res.stderr}"
    )
