r"""INFRA-01: local wine smoke loop for recovery/scripts/restore.bat.

This is a *development pre-filter*, NOT a gate.  It runs the real Windows
recovery driver (``restore.bat``) through wine's ``cmd`` against a fake
meta tree + data tree mapped onto wine drive letters, and asserts only
coarse outcomes (repo discovered / tier-1 invoked, expected error strings
on the sad paths).

Why coarse: wine's ``cmd`` is an *incomplete* reimplementation of the
Windows command interpreter.  Delayed expansion (``!VAR!``), the exact
``set /p`` stdin-consumption semantics, and ``for %%L in (...)`` drive
enumeration all differ subtly from real Windows.  A green run here means
"the script structure is probably right"; the *truth source* is the
Tier-3 GitHub job (`.github/workflows/windows-e2e.yml`) running on a real
``windows-latest`` runner.

Wine maps drive letters via symlinks under
``$WINEPREFIX/dosdevices/<letter>:`` — so ``ln -s <dir> .../e:`` makes
``if exist E:\data\`` in the .bat resolve against a Linux directory with
no optical hardware involved.

Skips honestly when wine is absent.  Each test uses a throwaway
WINEPREFIX under ``tmp_path`` so it never touches the shared
``/scratch/wine-prefix`` used by the binary-coverage harness.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
RESTORE_BAT = REPO_ROOT / "recovery" / "scripts" / "restore.bat"
RESTORE_EXE = REPO_ROOT / "recovery" / "bin" / "x86_64-windows" / "lcsas-restore.exe"

_WINE_OK = shutil.which("wine") is not None
_BAT_OK = RESTORE_BAT.is_file()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not (_WINE_OK and _BAT_OK),
        reason=(
            f"restore.bat wine smoke requires wine (present={_WINE_OK}) "
            f"and {RESTORE_BAT} (present={_BAT_OK})"
        ),
    ),
]

# wine cold-starts a prefix on first use; be generous so a loaded CI host
# does not flake the smoke loop.
TIMEOUT = 120


def _init_prefix(prefix: Path) -> dict[str, str]:
    """Create a throwaway WINEPREFIX and return the env to use with it."""
    prefix.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "WINEDEBUG": "-all",
        "WINEPREFIX": str(prefix),
        "DISPLAY": "",  # never pop an X11 window
    }
    # `wineboot --init` is slow but deterministic; a bare `wine cmd /c ver`
    # also bootstraps the prefix.  Use the latter — it is enough to create
    # the dosdevices/ tree we symlink into.
    subprocess.run(
        ["wine", "cmd", "/c", "ver"],
        env=env, capture_output=True, text=True, timeout=TIMEOUT,
    )
    return env


def _map_drive(prefix: Path, letter: str, target: Path) -> None:
    """Symlink ``<letter>:`` in the wine prefix to a Linux directory."""
    dosdevices = prefix / "dosdevices"
    dosdevices.mkdir(parents=True, exist_ok=True)
    link = dosdevices / f"{letter.lower()}:"
    if link.exists() or link.is_symlink():
        link.unlink()
    link.symlink_to(target, target_is_directory=True)


def _make_meta_tree(root: Path) -> None:
    """A meta-disc-shaped tree: recovery/scripts/restore.bat + a repo +
    the bundled Windows tier-1 binary at recovery/bin/<arch>/.

    restore.bat auto-discovers the recovery root one level up from its own
    location (``%~dp0..``), so we mirror the real on-disc layout:
    ``<root>/recovery/scripts/restore.bat`` with ``recovery/bin`` and
    ``recovery/repo`` beside it.
    """
    scripts = root / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")

    arch = "x86_64-pc-windows-gnu"
    bin_arch = root / "recovery" / "bin" / arch
    bin_arch.mkdir(parents=True)
    if RESTORE_EXE.is_file():
        shutil.copy2(RESTORE_EXE, bin_arch / "lcsas-restore.exe")

    repo = root / "recovery" / "repo"
    (repo / "keys").mkdir(parents=True)
    (repo / "index").mkdir()
    (repo / "data").mkdir()


def _run_bat(
    env: dict[str, str],
    meta_letter: str,
    *,
    stdin_data: str,
) -> subprocess.CompletedProcess[str]:
    """Drive restore.bat under wine with redirected stdin.

    LCSAS_NO_RELOCATE=1 keeps us on the direct flow (no copy-to-%TEMP%
    re-launch).  LCSAS_TARGET pins the target triple so detection cannot
    misfire under wine.
    """
    run_env = {
        **env,
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_TARGET": "x86_64-pc-windows-gnu",
    }
    bat = f"{meta_letter.upper()}:\\recovery\\scripts\\restore.bat"
    return subprocess.run(
        ["wine", "cmd", "/c", bat],
        input=stdin_data,
        env=run_env,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )


def test_happy_path_discovers_repo_and_invokes_tier1(tmp_path: Path) -> None:
    """With a well-formed meta tree, restore.bat should find the repo and
    reach the tier-1 invocation (printing the `[tier 1] running` banner).

    We do NOT assert a successful restore — the stub repo cannot decrypt
    anything — only that the script got *past* discovery to the binary,
    which is the step the broken-discovery dead-end (UX-01) fails at.
    """
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)

    meta = tmp_path / "meta"
    _make_meta_tree(meta)
    _map_drive(prefix, "e", meta)

    target = tmp_path / "restored"
    target.mkdir()

    # answers.txt equivalent via stdin: target dir, password, plus padding
    # so any stray `pause`/`set /p` read on an error branch never blocks.
    stdin_data = f"{target}\nhunter2\n\n\n\n\n"
    res = _run_bat(env, "e", stdin_data=stdin_data)

    out = res.stdout + res.stderr
    # Coarse outcome: discovery succeeded far enough to print the recovery
    # root + repo banner and reach the tier-1 binary invocation.
    assert "Repo:" in out or "[tier 1]" in out, (
        "restore.bat did not reach repo discovery / tier-1 under wine; "
        f"rc={res.returncode}\n--- output ---\n{out}"
    )


def test_missing_repo_reports_error(tmp_path: Path) -> None:
    """A meta tree with bin/ but no keys+index repo must hit the
    'no restic repo' error path, not silently proceed."""
    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)

    meta = tmp_path / "meta"
    scripts = meta / "recovery" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy2(RESTORE_BAT, scripts / "restore.bat")
    # bin/ present so arch detection passes, but NO repo (keys/+index/).
    (meta / "recovery" / "bin" / "x86_64-pc-windows-gnu").mkdir(parents=True)
    _map_drive(prefix, "e", meta)

    target = tmp_path / "restored"
    target.mkdir()
    stdin_data = f"{target}\nhunter2\n\n\n\n\n"
    res = _run_bat(env, "e", stdin_data=stdin_data)

    out = (res.stdout + res.stderr).lower()
    assert "could not find an lcsas backup set" in out, (
        "restore.bat did not report the missing-repo error under wine; "
        f"rc={res.returncode}\n--- output ---\n{res.stdout + res.stderr}"
    )
