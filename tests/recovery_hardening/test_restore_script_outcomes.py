"""Issue #887: the script contract table, run on restore.sh and restore.bat.

Each case in ``tests/contract/script_contract.py`` says what a reader
did (its exit code, its failure rows, its words) and what each script
must make of it: its own exit code, its message, whether it says how to
carry on, whether the tools folder stays and whether "Restore another
backup set?" is asked.

- ``test_restore_sh[<case>]`` runs restore.sh with stub readers.
- ``test_restore_bat[<case>]`` runs restore.bat under wine with a stub
  ``.exe``.  It skips without wine, or without a Windows C compiler
  (``x86_64-w64-mingw32-gcc`` or ``zig``) to build the stub; a case
  restore.bat has no counterpart of skips with the reason, and a case
  wine cannot show (``wine_gap``) is a strict xfail.

Named ``test_restore_*`` so ``make shell-coverage`` traces it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.contract.script_contract import (
    SCRIPT_CASES,
    NoStub,
    ScriptCase,
    WineRig,
    build_stub_exe,
    mismatches,
    run_restore_bat,
    run_restore_sh,
)
from tests.recovery_hardening._wine_helpers import (
    stop_and_remove_prefix,
    stop_wineserver,
)


def _id(case: ScriptCase) -> str:
    return case.name.replace(" ", "-")


def _report(case: ScriptCase, script: str, problems: list[str], output: str) -> str:
    return (f"{script}, case {case.name!r}:\n  " + "\n  ".join(problems)
            + "\n--- output ---\n" + output)


@pytest.mark.skipif(not sys.platform.startswith("linux"),
                    reason="restore.sh stubs are POSIX sh scripts")
@pytest.mark.parametrize("case", SCRIPT_CASES, ids=_id)
def test_restore_sh(case: ScriptCase, tmp_path: Path) -> None:
    got = run_restore_sh(case, tmp_path)
    problems = mismatches(case.sh, got)
    assert not problems, _report(case, "restore.sh", problems, got.output)


# ── restore.bat under wine ──────────────────────────────────────────


@pytest.fixture(scope="module")
def wine_rig(tmp_path_factory: pytest.TempPathFactory) -> Iterator[WineRig]:
    """One wine prefix (about 1.3 GB) and one stub .exe for every case."""
    if not shutil.which("wine"):
        pytest.skip("restore.bat cases need wine")
    base = tmp_path_factory.mktemp("bat-contract")
    try:
        stub = build_stub_exe(base / "stub" / "stub.exe")
    except NoStub as exc:
        pytest.skip(str(exc))
    # The same bounded wineboot probe as the other restore.bat suites
    # (#390): a wedged wineboot skips, it does not grind.
    with tempfile.TemporaryDirectory() as probe:
        env = {**os.environ, "WINEDEBUG": "-all", "WINEPREFIX": probe,
               "DISPLAY": ""}
        try:
            subprocess.run(["wine", "wineboot", "--init"], env=env,
                           capture_output=True, timeout=90)
        except subprocess.TimeoutExpired:
            pytest.skip("wineboot did not initialise a prefix within 90s (#390)")
        except OSError:
            pytest.skip("wine is not runnable on this host")
        finally:
            stop_wineserver(probe)
    from tests.recovery_hardening.test_restore_bat_wine_smoke import _init_prefix

    prefix = base / "wineprefix"
    rig = WineRig(prefix=prefix, env=_init_prefix(prefix), stub=stub)
    try:
        yield rig
    finally:
        stop_and_remove_prefix(prefix)


@pytest.mark.integration
@pytest.mark.parametrize("case", SCRIPT_CASES, ids=_id)
def test_restore_bat(case: ScriptCase, tmp_path: Path,
                     request: pytest.FixtureRequest) -> None:
    if case.bat is None:
        pytest.skip(f"no restore.bat case: {case.bat_why}")
    if case.wine_gap:
        request.applymarker(pytest.mark.xfail(strict=True, reason=case.wine_gap))
    rig: WineRig = request.getfixturevalue("wine_rig")
    got = run_restore_bat(case, tmp_path, rig)
    problems = mismatches(case.bat, got)
    assert not problems, _report(case, "restore.bat", problems, got.output)


def test_every_bat_gap_says_why() -> None:
    for case in SCRIPT_CASES:
        assert case.bat is not None or case.bat_why, case.name
