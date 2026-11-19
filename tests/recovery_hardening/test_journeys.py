"""The same restore journeys run through restore.sh and restore.bat, so the
two cannot drift apart where the person at the keyboard would notice (#895).

Each journey of ``tests/contract/journeys.py`` runs through
``restore.sh`` (under a pseudoterminal) and ``restore.bat`` (under wine),
against the same archive: real rustic sets, burned by the real pipeline
and unpacked into folders that stand in for discs, built once per
session.  Each run must ask the same questions in the same order (by
key phrase), end with the same exit code, restore the set byte-exact
and leave the tools folder as the journey says, except where the
journey records a difference (``differs``) and the reason for it.

Needs rustic and xorriso (the archives) and, for the restore.bat runs,
wine and the committed Windows programs; each part skips without them.
CI's ``recovery-hardening`` job (.github/workflows/test.yml) has all
four.  Not named ``test_restore_*.py``: ``make shell-coverage`` runs that
glob again, and these runs add no trace (restore.sh runs under a pty,
not through the traced ``subprocess.run``).
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from tests.contract.journeys import (
    ALTERED,
    ARCHIVES,
    JOURNEYS,
    Archive,
    Journey,
    alter_archive,
    build_archive,
    mismatches,
    run_journey_bat,
    run_journey_sh,
    writable,
)

# The wine prefix of the restore.bat journeys (one for the module; each
# run resets its drive letters, %TEMP%, the Desktop and the default
# tools folder).
from tests.recovery_hardening.test_restore_bat_journey_wine import (  # noqa: F401
    fresh,
    wine,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WIN_BIN = REPO_ROOT / "recovery" / "bin" / "x86_64-windows"

_ARCHIVE_TOOLS = all(shutil.which(t) for t in ("rustic", "xorriso"))
_WINE_OK = shutil.which("wine") is not None and all(
    (WIN_BIN / p).is_file()
    for p in ("lcsas-restore.exe", "lcsas-keyshare.exe", "lcsas-ecc.exe"))

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _ARCHIVE_TOOLS,
                       reason="the journey archives need rustic and xorriso"),
]


class _Archives:
    """Each archive of ARCHIVES or ALTERED, built the first time a
    journey needs it."""

    def __init__(self, base: Path) -> None:
        self._base = base
        self._built: dict[str, Archive] = {}

    def __getitem__(self, key: str) -> Archive:
        if key not in self._built:
            work = self._base / key.replace(" ", "-").replace(",", "")
            if key in ALTERED:
                alt = ALTERED[key]
                self._built[key] = alter_archive(self[alt.base], alt.how, work)
            else:
                self._built[key] = build_archive(ARCHIVES[key], work)
        return self._built[key]

    def close(self) -> None:
        for archive in self._built.values():
            for d in (archive.meta, *archive.discs.values()):
                writable(d)


@pytest.fixture(scope="session")
def archives(tmp_path_factory: pytest.TempPathFactory):
    built = _Archives(tmp_path_factory.mktemp("journey-archives"))
    try:
        yield built
    finally:
        built.close()


_bat_runs = [0]


def _check(journey: Journey, result, archive: Archive) -> None:
    bad = mismatches(journey, result, archive)
    then = ""
    if result.then is not None:
        then = (f"\n--- carrying on: output (last 4000 characters) ---\n"
                f"{result.then.output[-4000:]}")
    assert not bad, (
        f"{journey.name} through restore.{result.script} "
        f"({result.seconds:.0f} s):\n  " + "\n  ".join(bad)
        + f"\n--- output (last 6000 characters) ---\n{result.output[-6000:]}" + then)


@pytest.mark.parametrize("journey", JOURNEYS, ids=[j.name for j in JOURNEYS])
def test_journey_restore_sh(journey: Journey, archives: _Archives,
                            tmp_path: Path) -> None:
    archive = archives[journey.archive]
    result = run_journey_sh(journey, archive, tmp_path)
    _check(journey, result, archive)


@pytest.mark.skipif(not _WINE_OK,
                    reason="restore.bat journeys need wine and the committed "
                           "Windows programs")
@pytest.mark.parametrize("journey", JOURNEYS, ids=[j.name for j in JOURNEYS])
def test_journey_restore_bat(journey: Journey, archives: _Archives,
                             request: pytest.FixtureRequest) -> None:
    archive = archives[journey.archive]
    prefix = request.getfixturevalue("fresh")
    _bat_runs[0] += 1
    result = run_journey_bat(journey, archive, prefix, _bat_runs[0])
    _check(journey, result, archive)

