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

from tests.recovery_hardening._diff_helpers import non_marker_files

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


@pytest.fixture(scope="module", autouse=True)
def _require_working_wineboot() -> None:
    """Skip these tests if wineboot cannot initialise a fresh prefix within a
    bounded time.  On some hosts wineboot's first-run (Mono/Gecko) init hangs
    indefinitely (issue #390); each per-test wine call then burns the full
    TIMEOUT, wedging `make gate`'s shell-coverage phase for many minutes.  A
    single bounded probe fails fast to a clean skip instead of grinding.
    """
    if not _WINE_OK:
        return  # the module skipif already handles a missing wine
    import tempfile
    with tempfile.TemporaryDirectory() as _td:
        env = {**os.environ, "WINEDEBUG": "-all",
               "WINEPREFIX": _td, "DISPLAY": ""}
        try:
            subprocess.run(["wine", "wineboot", "--init"],
                           env=env, capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            pytest.skip(
                "wineboot did not initialise a fresh prefix within 90s on "
                "this host (issue #390) — skipping restore.bat wine tests"
            )
        except OSError:
            pytest.skip("wine is not runnable on this host")


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


def _make_meta_tree(root: Path, repo_src: Path | None = None) -> None:
    """A meta-disc-shaped tree: recovery/scripts/restore.bat + a repo +
    the bundled Windows tier-1 binary at recovery/bin/<arch>/.

    restore.bat auto-discovers the recovery root one level up from its own
    location (``%~dp0..``), so we mirror the real on-disc layout:
    ``<root>/recovery/scripts/restore.bat`` with ``recovery/bin`` and
    ``recovery/repo`` beside it.  ``repo_src`` copies a real repo (e.g.
    the decryptable fixture) in place of the default empty stub.
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
    if repo_src is not None:
        shutil.copytree(repo_src, repo)
    else:
        (repo / "keys").mkdir(parents=True)
        (repo / "index").mkdir()
        (repo / "data").mkdir()


def _run_bat(
    env: dict[str, str],
    meta_letter: str,
    *,
    stdin_data: str,
    extra_env: dict[str, str] | None = None,
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
        **(extra_env or {}),
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


def test_wrong_password_is_terminal_no_tier2_fallthrough(
    tmp_path: Path,
) -> None:
    """Issue #384: a tier-1 wrong-password exit (77) must STOP restore.bat
    -- no 'trying tier 2' fallthrough.  Every tier reads the same keys with
    the same password, and a later tier can leave a partial tree behind
    before it too rejects the password.

    Uses the real committed lcsas-restore.exe against the real fixture
    repo so the 77 comes from the genuine key-decrypt failure path, and
    plants a decoy rustic-static.exe so the no-fallthrough assertion has
    teeth (the tier-2 `if exist` is true).
    """
    fixture_repo = REPO_ROOT / "recovery" / "tests" / "fixtures" / "repo"
    if not RESTORE_EXE.is_file():
        pytest.skip(f"{RESTORE_EXE} not present")
    if not (fixture_repo / "keys").is_dir():
        pytest.skip("fixture repo not generated; run gen_fixture.py")

    prefix = tmp_path / "wineprefix"
    env = _init_prefix(prefix)

    meta = tmp_path / "meta"
    _make_meta_tree(meta, repo_src=fixture_repo)
    # Decoy tier 2: exists, so a fallthrough WOULD print its banner.
    bin_arch = meta / "recovery" / "bin" / "x86_64-pc-windows-gnu"
    (bin_arch / "rustic-static.exe").write_bytes(b"MZ decoy")
    _map_drive(prefix, "e", meta)

    # The target must be a Windows-visible path (cmd cannot mkdir a
    # /scratch/... Unix path).  Reuse the already-mapped E: drive — a
    # second post-boot drive letter is not reliably visible to wine's
    # mountmgr on every host.
    #
    # Wine's `set /p` cannot do sequential multi-line answers: the FIRST
    # prompt slurps the entire remaining stdin into one variable (CRLF
    # or LF alike; a real Windows console reads per line).  So: feed
    # exactly ONE LF-terminated line (the target — a lone line reads
    # cleanly), and deliver the password by pre-setting LCSAS_PW in the
    # environment — `set /p` at EOF keeps the variable's prior value,
    # which is also genuine cmd semantics on real Windows.
    target = meta / "restored"
    res = _run_bat(
        env, "e",
        stdin_data="E:\\restored\n",
        extra_env={"LCSAS_PW": "not-the-real-password"},
    )

    out = res.stdout + res.stderr
    assert res.returncode == 77, (
        "restore.bat must propagate tier-1's wrong-password exit 77; "
        f"rc={res.returncode}\n--- output ---\n{out}"
    )
    assert "could not decrypt the repository" in out.lower(), (
        f"operator-facing wrong-password banner missing:\n{out}"
    )
    assert "trying tier 2" not in out and "[tier 2] running" not in out, (
        f"restore.bat fell through to tier 2 on a wrong password:\n{out}"
    )
    # No restored data may be left behind (the resume sentinel is
    # excluded — see conftest.non_marker_files).
    leftovers = non_marker_files(target)
    assert leftovers == [], (
        f"wrong-password run left a partial tree: {leftovers}"
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
