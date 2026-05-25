"""Hardening test (issue #225): restore.sh terminal error UX when
the meta disc lacks ALL three tier binaries.

The worst-case scenario is a meta disc that carries no working
recovery binary at any tier: tier-1 (lcsas-restore), tier-2
(rustic-static), and tier-3 (python3 + standalone_restorer.py) are
all missing or unrunnable.  Before this guard, restore.sh prompted
for the operator's password, walked discovery, and then printed
"ERROR: no recovery method available." -- but only after exec was
already past the point of no return for the typed secret.

This test pins:
  * the guard fires BEFORE the password prompt (so secrets do not
    get typed into the void);
  * the documented exit code is 64;
  * the error message names each missing tier so the operator can
    audit the disc;
  * the error message points at the manual-recovery docs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"
HOST_TARGET = "x86_64-unknown-linux-musl"

EXIT_NO_RECOVERY_BIN = 64


def _make_minimal_repo(recovery: Path) -> Path:
    repo = recovery / "metadata" / "alpha"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    return repo


def _stub_recovery(tmp_path: Path) -> Path:
    """Build a fixture that mimics a stripped-down meta disc:
    has a repo + recovery/bin/<target>/ but ZERO binaries inside it,
    and no standalone_restorer.py reachable from the recovery tree."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    # Empty bin/<target>/ — both tier-1 and tier-2 binaries absent.
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_minimal_repo(recovery)
    return recovery


def _python_free_path(tmp_path: Path) -> str:
    """A PATH that has the shell tools restore.sh needs (sh, mktemp,
    grep, awk, basename, etc.) but NO python3 / python.  Built by
    keeping /bin and /usr/bin entries and prepending a shim dir whose
    `python3` / `python` files exit 127.

    Without this hedge, restore.sh's tier-3 probe (`command -v
    python3`) would find the host's Python and the guard would not
    fire even though the test intends to simulate 'no tier 3'."""
    shim = tmp_path / "python_shim"
    shim.mkdir()
    for name in ("python", "python3"):
        p = shim / name
        p.write_text("#!/bin/sh\nexit 127\n")
        p.chmod(0o755)
    # Keep system /bin and /usr/bin so sh / mktemp / grep / awk work.
    return f"{shim}:/usr/bin:/bin"


def test_all_tiers_missing_exits_with_documented_code(tmp_path: Path) -> None:
    recovery = _stub_recovery(tmp_path)
    target = tmp_path / "restored"

    env = {
        # Inherit the bare minimum environment.  PATH is scrubbed of
        # python so tier-3's command -v probe fails.
        "HOME": str(tmp_path),
        "PATH": _python_free_path(tmp_path),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        # If a stray LCSAS_PASSWORD leaked through it would not affect
        # the guard (which fires before the prompt) -- but force the
        # path through interactive to prove the guard fires first.
    }
    # Intentionally do NOT feed stdin; if the guard fires correctly
    # the script exits before asking for a password.
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="", capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == EXIT_NO_RECOVERY_BIN, (
        f"expected documented exit code {EXIT_NO_RECOVERY_BIN}; got "
        f"{res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    # Headline message verbatim per issue #225.
    assert "meta disc lacks all recovery binaries" in res.stderr, (
        f"missing headline message in stderr:\n{res.stderr}"
    )
    # Tier-by-tier diagnostic.
    assert "tier 1:" in res.stderr, res.stderr
    assert "lcsas-restore" in res.stderr, res.stderr
    assert "tier 2:" in res.stderr, res.stderr
    assert "rustic-static" in res.stderr, res.stderr
    assert "tier 3:" in res.stderr, res.stderr
    # Manual-recovery pointer present.
    assert "RECOVER.txt" in res.stderr, res.stderr
    # Exit code is named in the message so the operator can find it
    # by grepping the source.
    assert "64" in res.stderr, res.stderr
    # CRITICAL: no password prompt fired.  If the guard ran before
    # the prompt, "Password:" never appears.
    assert "Password:" not in res.stderr, (
        "guard must fire BEFORE the password prompt; saw 'Password:' "
        f"in stderr:\n{res.stderr}"
    )


def test_python_tier_disabled_treated_as_missing(tmp_path: Path) -> None:
    """LCSAS_ALLOW_PYTHON_TIER=0 disables tier 3.  If tier-1 and
    tier-2 are also missing, the guard must fire."""
    recovery = _stub_recovery(tmp_path)
    target = tmp_path / "restored"

    # Even though the host has python3 on PATH, the env flag must
    # cause the guard to treat tier 3 as unavailable.
    env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_ALLOW_PYTHON_TIER": "0",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="", capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == EXIT_NO_RECOVERY_BIN, (
        f"expected {EXIT_NO_RECOVERY_BIN}; got {res.returncode}\n"
        f"stderr:{res.stderr}"
    )
    assert "LCSAS_ALLOW_PYTHON_TIER=0" in res.stderr, (
        f"diagnostic should call out the env flag; stderr:\n{res.stderr}"
    )


def test_guard_does_not_fire_when_tier3_is_available(tmp_path: Path) -> None:
    """If tier-3 (python + standalone_restorer.py) IS available, the
    guard must NOT fire even if tier-1 and tier-2 are absent -- the
    tier cascade should reach tier 3 as designed."""
    recovery = _stub_recovery(tmp_path)
    # Place a stub standalone_restorer.py where tier-3 looks for it.
    (recovery.parent / "standalone_restorer.py").write_text("# stub\n")
    # Stub python3 on PATH so the tier-3 probe sees it.
    stub_dir = tmp_path / "stubbin"
    stub_dir.mkdir()
    (stub_dir / "python3").write_text(
        "#!/bin/sh\necho TIER3_RAN\nexit 0\n"
    )
    (stub_dir / "python3").chmod(0o755)

    target = tmp_path / "restored"
    env = {
        "HOME": str(tmp_path),
        "PATH": f"{stub_dir}:" + os.environ.get("PATH", ""),
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
        "LCSAS_PASSWORD": "stub",  # don't block on prompt
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        input="", capture_output=True, text=True, env=env, timeout=15,
    )
    assert res.returncode == 0, (
        f"guard should not fire when tier 3 is reachable; "
        f"rc={res.returncode}\nstdout:{res.stdout}\nstderr:{res.stderr}"
    )
    assert "TIER3_RAN" in res.stdout, (
        f"tier 3 should have run; stdout:\n{res.stdout}"
    )
    assert "meta disc lacks all recovery binaries" not in res.stderr, (
        f"guard fired spuriously:\n{res.stderr}"
    )
