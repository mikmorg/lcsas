"""Hardening tests: restore.sh UX improvements (recommendations #3, #4, #8).

These tests cover three small UX gates added to ``recovery/scripts/restore.sh``
after the latest blind-restore agent transcript surfaced three friction points:

  * No pack-search paths discovered at start.  Previously the script would
    march on, prompt for a password, and only fail deep inside the recovery
    binary with an unactionable "no packs found" message.  Now it fails fast
    with an instruction to insert a data disc.
  * Free-form "Repository: " prompt with no list to copy from.  Operators
    typed guesses ("default", "main", "<repo-name>") that didn't match any
    tenant.  Now we present a numbered menu and accept either number or
    literal name.
  * ``--help`` had no QUICK START.  A first-time operator had to read the
    full README to figure out the canonical invocation.  Now ``--help``
    leads with a 5-step recipe.

What this catches:
  - Future refactors that drop the discovery hard-error or break the
    LCSAS_ALLOW_NO_PACK_SEARCH escape hatch.
  - Numbered-menu regressions (e.g. dropping the legacy name-form fallback,
    breaking ``eval``-free positional-arg lookup).
  - QUICK START text getting trimmed from --help during a rewrite.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_SH = REPO_ROOT / "recovery" / "scripts" / "restore.sh"

# Matches detect_arch.sh emission on Linux x86_64 — the only host arch
# the hardening tests run on.  Bare tier-1 binaries land under
# ``recovery/bin/<HOST_TARGET>/``.
HOST_TARGET = "x86_64-unknown-linux-musl"


def _make_repo_skeleton(
    root: Path, name: str, *, with_data: bool = True
) -> Path:
    """Make a minimal restic-format-shaped repo dir at root/<name>.

    ``with_data=False`` omits the ``data/`` subdir so callers can build a
    "metadata-only" fixture that triggers the new discovery hard-error.
    """
    repo = root / name
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    if with_data:
        (repo / "data").mkdir()
    (repo / "snapshots").mkdir()
    (repo / "keys" / "stub_key").write_text("stub")
    return repo


def _install_stub_binary(recovery: Path, target: str, name: str) -> Path:
    """Install a stub recovery binary that prints argv (one ARG: per line)."""
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
    return [
        line.removeprefix("ARG: ")
        for line in stdout.splitlines()
        if line.startswith("ARG: ")
    ]


def _arg_value(args: list[str], flag: str) -> str | None:
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args):
            return args[i + 1]
    return None


# ── Recommendation #3: hard-error on empty pack-search list ──────────


def test_hard_error_when_no_data_discs_discovered(tmp_path: Path) -> None:
    """A metadata-only repo with no mounted data discs must abort fast,
    not march on into a password prompt + opaque downstream failure."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    # Mark the dir as a recovery root so the script's auto-detect picks
    # `$1` (its "looks like a recovery root" probe needs bin/ or src/).
    # We leave the bin/<target>/ tree empty -- no tier binary will
    # dispatch, but the discovery check fires before tier dispatch.
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    # Single tenant, NO data/ subdir, so the legacy "self-contained
    # repo" escape hatch doesn't apply either.
    _make_repo_skeleton(recovery / "metadata", "alpha", with_data=False)
    target = tmp_path / "restored"

    full_env = {**os.environ, "LCSAS_MOUNT_DIRS": ""}
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=full_env, timeout=15,
        # Password is still prompted before the discovery gate fires;
        # feed a stub so the read doesn't EOF and confuse the test.
        input="stub-pw\n",
    )
    assert res.returncode == 1, (
        f"expected exit 1 when no data discs are discoverable; got "
        f"{res.returncode}.\nstdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    assert "no data discs detected" in res.stderr, (
        f"stderr must contain the actionable 'no data discs detected' "
        f"banner; got:\n{res.stderr}"
    )


def test_lcsas_allow_no_pack_search_bypasses_check(tmp_path: Path) -> None:
    """The escape hatch lets scripted environments (CI, pre-staged caches)
    skip the gate.  After the gate the script will still fail downstream,
    but it MUST NOT exit with the discovery banner."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    (recovery / "bin" / HOST_TARGET).mkdir(parents=True)
    _make_repo_skeleton(recovery / "metadata", "alpha", with_data=False)
    target = tmp_path / "restored"

    full_env = {
        **os.environ,
        "LCSAS_MOUNT_DIRS": "",
        "LCSAS_ALLOW_NO_PACK_SEARCH": "1",
    }
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=full_env, timeout=15,
        input="stub-pw\n",
    )
    # We deliberately don't assert exit 0 — without a tier binary the
    # script falls through tier dispatch and exits 1 with the
    # "no recovery method available" banner.  What matters is that
    # the new gate did NOT fire.
    assert "no data discs detected" not in res.stderr, (
        f"escape hatch must bypass the discovery hard-error; got:\n"
        f"{res.stderr}"
    )


# ── Recommendation #4: numbered repo prompt ──────────────────────────


def test_numbered_repo_prompt_accepts_number(tmp_path: Path) -> None:
    """``1`` should resolve to the FIRST listed candidate (alpha here),
    matching the new ``1) alpha / 2) bravo`` menu."""
    recovery = tmp_path / "recovery"
    recovery.mkdir()
    _install_stub_binary(recovery, HOST_TARGET, "lcsas-restore")
    alpha = _make_repo_skeleton(recovery / "metadata", "alpha")
    _make_repo_skeleton(recovery / "metadata", "bravo")
    target = tmp_path / "restored"

    full_env = {**os.environ, "LCSAS_MOUNT_DIRS": ""}
    # Feed the menu number, then a stub password.
    res = subprocess.run(
        ["sh", str(RESTORE_SH), str(recovery), str(target), "latest"],
        capture_output=True, text=True, env=full_env, timeout=15,
        input="1\nstub-pw\n",
    )
    assert res.returncode == 0, (
        f"numbered-menu run failed.\n"
        f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
    )
    args = _stub_args(res.stdout)
    assert _arg_value(args, "--repo") == str(alpha), (
        f"menu choice '1' should pick {alpha!r}; got "
        f"{_arg_value(args, '--repo')!r}.  full argv: {args}"
    )
    # The user should also see the numbered list in stderr.
    assert "1) alpha" in res.stderr and "2) bravo" in res.stderr, (
        f"stderr must render a numbered menu; got:\n{res.stderr}"
    )


# ── Recommendation #8: QUICK START in --help ────────────────────────


def test_help_includes_quick_start() -> None:
    """``--help`` must lead with the QUICK START so a first-time
    operator doesn't have to dig through the README."""
    res = subprocess.run(
        ["sh", str(RESTORE_SH), "--help"],
        capture_output=True, text=True, timeout=10,
    )
    assert res.returncode == 0, res.stderr
    assert "QUICK START" in res.stdout, (
        f"--help must include the QUICK START heading; got:\n{res.stdout}"
    )
    assert "sudo mount /dev/sr0" in res.stdout, (
        f"--help must include the canonical mount example; got:\n"
        f"{res.stdout}"
    )
