"""Hardening test #3: restore.sh repo discovery on canonical layouts.

Before this commit the script only looked at `${RECOVERY}/repo/{keys,
index}` and `${RECOVERY}/{keys,index}` — neither of which the
production meta disc actually ships.  Modern meta discs ship per-tenant
repos under `${RECOVERY}/../metadata/<tenant>/{keys,index}` and rely on
the operator mounting a data disc at `/mnt` (Linux) or `/Volumes/*`
(macOS) when packs span multiple discs.

This test exercises every layout the script must support:

  • single-tenant at $RECOVERY/metadata/<one>/ → auto-pick.
  • multi-tenant at $RECOVERY/metadata/{alpha,bravo}/ + LCSAS_REPO env
    → pick by name.
  • multi-tenant at $RECOVERY/metadata/{alpha,bravo}/ + no LCSAS_REPO
    and stdin closed → exit 1, list both candidates in stderr.
  • legacy $RECOVERY/repo/{keys,index} → back-compat works.

Each case uses a stub `lcsas-restore` that prints its argv to stdout
and exits 0 — we read `--repo X` out of that to confirm the script
picked the right repo.

What this catches:
  - Future "simplification" of the discovery logic that breaks back-compat
    with /repo/ legacy layouts (still used by restore_legacy.sh's repos).
  - Forgetting to add a new search path when the production layout
    changes (e.g. /run/media/$USER/* on systemd-mount).
  - LCSAS_REPO env getting renamed without updating discovery.
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
    """Install a stub recovery binary at `recovery/bin/<target>/<name>`.

    The stub just prints its argv to stdout (one arg per line, prefixed
    `ARG: `) so tests can assert the script invoked it with the
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


def _run_restore(recovery: Path, target_dir: Path, env: dict[str, str],
                 password: str = "stub", repo_input: str | None = None,
                 ) -> subprocess.CompletedProcess:
    """Run restore.sh with the explicit RECOVERY_ROOT TARGET_DIR form.

    Feeds repo selection (if any) first, then password.  Tests
    isolate the script from real mounted media by setting
    LCSAS_MOUNT_DIRS="" so /Volumes /media /mnt aren't scanned.
    """
    stdin = ""
    if repo_input is not None:
        stdin += repo_input + "\n"
    stdin += password + "\n"
    full_env = {**os.environ, "LCSAS_MOUNT_DIRS": "", **env}
    return subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target_dir), "latest"],
        input=stdin, capture_output=True, text=True,
        env=full_env, timeout=15,
    )


def _stub_args(stdout: str) -> list[str]:
    """Extract the ARG: lines printed by the stub binary."""
    return [
        line.removeprefix("ARG: ")
        for line in stdout.splitlines()
        if line.startswith("ARG: ")
    ]


def _arg_value(args: list[str], flag: str) -> str | None:
    """Return the value of `--flag X` in the arg list, or None."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


# ── Single-tenant ────────────────────────────────────────────────────


def test_single_tenant_metadata_dir_is_found(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    repo = _make_repo_skeleton(recovery / "metadata", "alpha")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={})
    assert res.returncode == 0, (
        f"restore.sh failed for single-tenant layout.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(repo), (
        f"--repo passed to tier-1 was {_arg_value(args, '--repo')!r}, "
        f"expected {str(repo)!r}.  stub argv: {args}"
    )


# ── Multi-tenant ─────────────────────────────────────────────────────


def test_multi_tenant_env_var_selects(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    alpha = _make_repo_skeleton(recovery / "metadata", "alpha")
    _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={"LCSAS_REPO": "alpha"})
    assert res.returncode == 0, res.stderr
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(alpha), (
        f"LCSAS_REPO=alpha should pick {alpha!r}; got "
        f"{_arg_value(args, '--repo')!r}."
    )


def test_multi_tenant_prompt_selects(tmp_path: Path) -> None:
    """When LCSAS_REPO is unset, the script should prompt and accept
    the tenant name interactively (matching the human-in-chair UX)."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    bravo = _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={}, repo_input="bravo")
    assert res.returncode == 0, res.stderr
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(bravo), (
        f"Interactive choice 'bravo' should pick {bravo!r}; got "
        f"{_arg_value(args, '--repo')!r}."
    )


def test_multi_tenant_invalid_lcsas_repo_errors(tmp_path: Path) -> None:
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    _make_repo_skeleton(recovery / "metadata", "alpha")
    _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={"LCSAS_REPO": "charlie"})
    assert res.returncode != 0
    # The error message should name what's available.
    assert "alpha" in res.stderr and "bravo" in res.stderr, (
        f"error message must list available tenants; got:\n{res.stderr}"
    )


# ── Legacy back-compat ───────────────────────────────────────────────


def test_legacy_repo_dir_still_works(tmp_path: Path) -> None:
    """${RECOVERY}/repo/{keys,index} is the legacy layout used by
    restore_legacy.sh and the per-disc 'restic-style' assembly some
    operators do by hand.  Discovery must keep finding it."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    repo = _make_repo_skeleton(recovery, "repo")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={})
    assert res.returncode == 0, res.stderr
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(repo), (
        f"legacy /repo/ layout was not picked; got "
        f"{_arg_value(args, '--repo')!r}"
    )


# ── Empty case ───────────────────────────────────────────────────────


def test_no_repos_fails_with_actionable_error(tmp_path: Path) -> None:
    """Zero repos under the recovery tree → exit 1 with a hint about
    inserting a data disc."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    target = tmp_path / "restored"

    res = _run_restore(recovery, target, env={})
    assert res.returncode != 0
    # Message should be actionable — mention mount or insert.
    assert any(
        kw in res.stderr.lower()
        for kw in ("mount", "insert", "data disc")
    ), f"error message missing actionable hint; got:\n{res.stderr}"
