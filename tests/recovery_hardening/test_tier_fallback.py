"""Hardening test #10: restore.sh tier fallback under
LCSAS_TIER_FALLBACK=1.

The v3 blind run revealed that `restore.sh` `exec`s tier 1 and has
no fall-through if tier 1 crashes mid-restore.  The agent worked
around it by `mv lcsas-restore lcsas-restore.broken` — a real
human-in-chair wouldn't.  The user picked an opt-in fix:
`LCSAS_TIER_FALLBACK=1` runs each non-last tier as a subprocess
and falls through to the next tier on non-zero exit.

This test pins three properties of the opt-in fallback:

  • Without LCSAS_TIER_FALLBACK, a crashing tier 1 kills the run
    (default `exec` behavior — preserves the bare-minimum recovery
    story where tier 1 IS the recovery).
  • With LCSAS_TIER_FALLBACK=1 and a crashing tier 1, the script
    falls through to tier 2.
  • With LCSAS_TIER_FALLBACK=1 and tier 1 + tier 2 both crashing,
    the script reaches tier 3.

The companion verify.sh check #15 ("agent did not rename recovery
binaries") closes the loophole on the agent side; this test closes
it on the production-script side.
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
                            exit_code: int = 17) -> Path:
    """A stub that prints a tier-failure banner and exits non-zero."""
    bin_dir = recovery / "bin" / HOST_TARGET
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / name
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/sh
        echo "FAKE {name}: simulated crash" >&2
        exit {exit_code}
    """))
    stub.chmod(0o755)
    return stub


def _install_succeeding_binary(recovery: Path, name: str) -> Path:
    """A stub that prints `SUCCESS_<NAME>` and exits 0."""
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


def _make_repo(recovery: Path) -> None:
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()


def _run(recovery: Path, target_dir: Path, env_extra: dict[str, str],
         ) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        **env_extra,
    }
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )


def test_default_no_fallback_on_tier1_crash(tmp_path: Path) -> None:
    """Default behavior: tier 1 crash kills the run.  This preserves
    the bare-minimum recovery story — tier 1 IS the recovery, and
    failures must surface to the operator immediately."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={})
    # tier 1 was exec'd → its exit code propagates.
    assert res.returncode == 17, (
        f"default behavior should propagate tier-1 exit; got "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" not in res.stdout, (
        "without LCSAS_TIER_FALLBACK, tier 2 must NOT run after "
        "tier 1 — that would change default semantics."
    )


def test_fallback_to_tier2_on_tier1_crash(tmp_path: Path) -> None:
    """LCSAS_TIER_FALLBACK=1: tier 1 crashes → tier 2 runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={"LCSAS_TIER_FALLBACK": "1"})
    assert res.returncode == 0, (
        f"fallback should reach tier 2 and succeed; got "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "SUCCESS_rustic-static" in res.stdout, (
        f"tier 2 stub did not run; stdout:\n{res.stdout}"
    )
    assert "falling through to tier 2" in res.stderr, (
        f"diagnostic banner missing; stderr:\n{res.stderr}"
    )


def test_fallback_to_tier3_when_tier1_and_tier2_crash(tmp_path: Path) -> None:
    """Both tier 1 and tier 2 crash → tier 3 runs."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_failing_binary(recovery, "lcsas-restore", exit_code=17)
    _install_failing_binary(recovery, "rustic-static", exit_code=19)
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")

    # Stub python3 that just prints a marker and exits 0.
    pybin_dir = tmp_path / "stubbin"
    pybin_dir.mkdir()
    (pybin_dir / "python3").write_text(textwrap.dedent("""\
        #!/bin/sh
        echo "TIER3_PYTHON_RAN: $*"
        exit 0
    """))
    (pybin_dir / "python3").chmod(0o755)

    target = tmp_path / "restored"
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_TIER_FALLBACK": "1",
        "PATH": f"{pybin_dir}:" + os.environ.get("PATH", ""),
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="stub-password\n",
        capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"tier 3 should run and succeed; rc={res.returncode}\n"
        f"stdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "TIER3_PYTHON_RAN" in res.stdout, res.stdout
    assert "falling through to tier 3" in res.stderr, (
        f"tier-2-to-3 diagnostic missing:\n{res.stderr}"
    )


def test_fallback_preserves_success_when_tier1_works(tmp_path: Path) -> None:
    """LCSAS_TIER_FALLBACK=1 + tier 1 succeeds → exit 0, no tier 2."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _make_repo(recovery)
    _install_succeeding_binary(recovery, "lcsas-restore")
    _install_succeeding_binary(recovery, "rustic-static")
    target = tmp_path / "restored"

    res = _run(recovery, target, env_extra={"LCSAS_TIER_FALLBACK": "1"})
    assert res.returncode == 0, res.stderr
    assert "SUCCESS_lcsas-restore" in res.stdout
    assert "SUCCESS_rustic-static" not in res.stdout, (
        "tier 2 ran after a successful tier 1 — the fallback path "
        "is short-circuiting incorrectly."
    )
