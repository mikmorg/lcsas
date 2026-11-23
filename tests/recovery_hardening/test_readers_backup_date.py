"""Both readers say which backup they restore, by its date (#952 b,
#966 item 2).

- Before the restore: "Restoring the backup of 2026-11-21 14:02, the
  newest on these discs."  An older backup, chosen with ``--snapshot``,
  is named with the newest beside it.
- At the end of a restore that worked: "The backup of 2026-11-21 14:02
  is restored."
- The date is the snapshot's ``time`` as rustic stored it: the date and
  the minute of the backup computer's own clock, not converted.

Real rustic makes the backup set (two backups, at set times with UTC
offsets); tier 1 is the built ``lcsas-restore`` (``LCSAS_RESTORE_BIN``,
else ``recovery/build``, else the committed binary) and the Python
reader the generated ``python_reader.py``.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.contract.restore_contract import Skip as ContractSkip
from tests.contract.restore_contract import tier1_binary, tier3_script

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.skipif(not sys.platform.startswith("linux"),
                       reason="the committed tier-1 binary is Linux"),
]

PASSWORD = "test-password"
READERS = ("tier1", "tier3")


def _rustic(repo: Path, pw: Path, *args: str) -> str:
    return subprocess.run(
        ["rustic", "-r", str(repo), "--no-cache", "--password-file", str(pw),
         *args],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout


def _date(time: str) -> str:
    """The date line's text for a stored snapshot time: its own date and
    minute (the time is what rustic wrote, so the test does not depend
    on how rustic writes the offset)."""
    m = re.match(r"(\d{4}-\d\d-\d\d)[T ](\d\d:\d\d)", time)
    assert m, time
    return f"{m.group(1)} {m.group(2)}"


@pytest.fixture(scope="module")
def backups(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, list[tuple[str, str]]]:
    """A set with two backups; (repo, password file, [(id, date)] oldest
    first)."""
    work = tmp_path_factory.mktemp("dates")
    pw = work / "pw"
    pw.write_text(PASSWORD + "\n")
    src = work / "src"
    src.mkdir()
    repo = work / "repo"
    _rustic(repo, pw, "init")
    (src / "a.txt").write_text("first\n")
    _rustic(repo, pw, "backup", str(src), "--time", "2026-11-20 09:15:00+0100")
    (src / "a.txt").write_text("second\n")
    _rustic(repo, pw, "backup", str(src), "--time", "2026-11-21 14:02:33-0500")
    ids = re.findall(r"[0-9a-f]{64}", _rustic(repo, pw, "list", "snapshots"))
    found = sorted((json.loads(_rustic(repo, pw, "cat", "snapshot", i))["time"], i)
                   for i in ids)
    assert len(found) == 2, found
    return repo, pw, [(sid, _date(t)) for t, sid in found]


def _reader(reader: str, work: Path) -> list[str]:
    try:
        if reader == "tier1":
            return [str(tier1_binary())]
        return [sys.executable, "-I", str(tier3_script(work))]
    except ContractSkip as exc:
        pytest.skip(str(exc))


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}


def _restore(reader: str, repo: Path, pw: Path, target: Path,
             *extra: str) -> subprocess.CompletedProcess[str]:
    cmd = [*_reader(reader, target.parent), "--repo", str(repo),
           "--password-file", str(pw), "--restore-folder", str(target),
           "--interactive", "off", *extra]
    return subprocess.run(cmd, capture_output=True, text=True, env=_env(),
                          timeout=300)


@pytest.mark.parametrize("reader", READERS)
def test_the_newest_backup_is_named_by_its_date(
        reader: str, backups: tuple[Path, Path, list[tuple[str, str]]],
        tmp_path: Path) -> None:
    repo, pw, snaps = backups
    newest = snaps[-1][1]
    res = _restore(reader, repo, pw, tmp_path / "out")
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    start = f"Restoring the backup of {newest}, the newest on these discs."
    end = f"The backup of {newest} is restored."
    assert start in out, out
    assert end in out, out
    assert out.index(start) < out.index(end), out


@pytest.mark.parametrize("reader", READERS)
def test_an_older_backup_is_named_with_the_newest(
        reader: str, backups: tuple[Path, Path, list[tuple[str, str]]],
        tmp_path: Path) -> None:
    repo, pw, snaps = backups
    (old_id, old), (_new_id, newest) = snaps
    res = _restore(reader, repo, pw, tmp_path / "out", "--snapshot", old_id[:8])
    out = res.stdout + res.stderr
    assert res.returncode == 0, out
    assert (f"Restoring the backup of {old}; the newest on these discs "
            f"is of {newest}.") in out, out
    assert f"The backup of {old} is restored." in out, out
    assert "the newest on these discs." not in out, out


@pytest.mark.parametrize("reader", READERS)
def test_a_restore_that_stops_does_not_say_restored(
        reader: str, backups: tuple[Path, Path, list[tuple[str, str]]],
        tmp_path: Path) -> None:
    """Every pack of file data gone, no disc prompt: the file is not
    restored (exit 4), so the end does not say the backup is restored."""
    import shutil

    from tests.contract.restore_contract import _index

    repo, pw, snaps = backups
    copy = tmp_path / "repo"
    shutil.copytree(repo, copy)
    gone = [p for p, blobs in _index(copy, pw).items()
            if all(t == "data" for _b, t, _o, _n in blobs)]
    assert gone
    for pack in gone:
        (copy / "data" / pack[:2] / pack).unlink()
    res = _restore(reader, copy, pw, tmp_path / "out")
    out = res.stdout + res.stderr
    newest = snaps[-1][1]
    assert res.returncode == 4, out
    assert f"Restoring the backup of {newest}" in out, out
    assert "is restored." not in out, out
