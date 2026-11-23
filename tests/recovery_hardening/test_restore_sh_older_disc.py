"""restore.sh and a disc that is older, or of another archive (#952).

An archive burned in two sessions (``tests/contract/journeys.py``'s
``build_sessions``: real rustic, the owner's own commands): the first
data disc knows one backup, the second disc and the META disc two.

- A data disc whose snapshot list is a strict subset of the copy in
  hand (the META disc's) is no longer used without a word: a person is
  asked (S keeps the copy); with nobody to answer the disc is used, and
  a note says so.
- A data disc none of whose backup sets is this archive's is refused:
  its sets are not listed, its catalog not taken.  With a META disc that
  carries no sets, the first data disc accepted defines the archive.

The questions run through a pipe with ``LCSAS_INTERACTIVE=on`` (so
``make shell-coverage`` traces them); the journeys in
``tests/contract/journeys.py`` run the same under a terminal, both
scripts.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.contract.journeys import (
    FIRST,
    SESSIONS,
    Archive,
    build_sessions,
    writable,
)

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic,
              pytest.mark.requires_xorriso]

DOC = "documents"


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("sessions")
    built = build_sessions(SESSIONS["two sessions, another archive's disc"], work)
    runnable_in_place(built.meta)
    yield built
    for d in (built.meta, *built.discs.values(), *built.foreign.values()):
        writable(d)


def runnable_in_place(meta: Path) -> None:
    """The META disc's programs executable, as on a mounted disc: the
    journeys' folders drop every x bit, and these runs do not relocate
    (which copies the programs and sets it)."""
    for f in (meta / "recovery" / "bin").rglob("*"):
        if f.is_file():
            f.chmod(0o555)


def _run(archive: Archive, tmp: Path, discs: list[Path], *, stdin: str = "",
         interactive: str = "on", meta: Path | None = None,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    mnt = tmp / "mnt"
    mnt.mkdir()
    for d in discs:
        os.symlink(d, mnt / d.name)
    for sub in ("home", "tmp"):
        (tmp / sub).mkdir()
    pw = tmp / "pw.txt"
    pw.write_text(archive.sets[DOC].password)
    meta = meta or archive.meta
    full = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp / "home"),
        "TMPDIR": str(tmp / "tmp"),
        "LCSAS_MOUNT_DIRS": str(mnt),
        # In place, not relocated: the copy that relocation starts runs
        # under /bin/sh, which make shell-coverage cannot trace.
        "LCSAS_NO_RELOCATE": "1",
        "LCSAS_TOOLS_DIR": str(tmp / "tools"),
        "LCSAS_NEWEST_DISC_PROMPT": "0",
        "LCSAS_ANOTHER_SET": "0",
        "LCSAS_INTERACTIVE": interactive,
        "LCSAS_ALLOW_TMPFS_RESTORE_FOLDER": "1",
        **(env or {}),
    }
    return subprocess.run(
        ["sh", str(meta / "restore.sh"), "--password-file", str(pw),
         "--restore-folder", str(tmp / "out")],
        input=stdin, capture_output=True, text=True, env=full, timeout=300,
    )


def _shas(folder: Path) -> set[str]:
    return {hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


def _oldest(archive: Archive) -> Path:
    return archive.disc("oldest")


def test_older_disc_with_nobody_to_answer_is_used_and_said(
        archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, [_oldest(archive)], interactive="off")
    assert res.returncode == 0, res.stderr[-3000:]
    assert ("This disc carries 1 backup of 'documents'; the copy from the "
            "META disc knows 2.") in res.stderr
    assert "Nobody is here to answer, so this disc" in res.stderr
    assert set(archive.sets[FIRST.format(DOC)].manifest.values()) <= _shas(tmp_path / "out")


def test_older_disc_s_keeps_the_meta_discs_copy(archive: Archive, tmp_path: Path) -> None:
    """S: the set's copy used is the META disc's, not the disc's.  The
    restore then needs the newer disc, which is not here, so the run is
    stopped at the disc question (input ended)."""
    res = _run(archive, tmp_path, [_oldest(archive)], stdin="s\n")
    assert "ENTER (this disc), S (the copy) or Q: " in res.stderr
    assert "Using that copy" in res.stderr
    repo_id = archive.sets[DOC].repo_id
    assert (f"using backup set folder {archive.meta}/metadata/{repo_id}"
            in res.stderr), res.stderr[-3000:]
    # Kept for the next set and a resumed run.
    assert res.returncode != 0
    kept = tmp_path / "tools" / "metadata" / ".kept-sets"
    assert kept.read_text().split() == [repo_id]


def test_older_disc_q_stops(archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, [_oldest(archive)], stdin="x\nq\n")
    assert res.returncode == 65, res.stderr[-3000:]
    assert "Press ENTER, or type S or Q, then ENTER." in res.stderr


def test_older_disc_enter_uses_the_disc(archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, [_oldest(archive)], stdin="\n")
    assert res.returncode == 0, res.stderr[-3000:]
    assert set(archive.sets[FIRST.format(DOC)].manifest.values()) <= _shas(tmp_path / "out")


def test_a_disc_of_another_archive_is_refused(archive: Archive, tmp_path: Path) -> None:
    foreign = archive.disc("foreign")
    res = _run(archive, tmp_path, [*archive.discs.values(), foreign], interactive="off",
               env={"LCSAS_BACKUP_SET": "photos"})
    assert res.returncode == 1, res.stderr[-3000:]
    assert f"The disc labelled {foreign.name} belongs to a" in res.stderr
    (listed,) = [ln for ln in res.stderr.splitlines() if "not among" in ln]
    assert listed.endswith(f"backup sets: documents (id {archive.sets[DOC].repo_id})")
    # Its catalog is not taken either.
    assert f"using catalog {tmp_path / 'mnt' / foreign.name}" not in res.stderr


def test_a_meta_disc_with_no_sets_takes_them_from_the_first_data_disc(
        archive: Archive, tmp_path: Path) -> None:
    meta = tmp_path / "meta" / "LCSAS_META"
    shutil.copytree(archive.meta, meta, symlinks=True)
    writable(meta)
    shutil.rmtree(meta / "metadata")
    runnable_in_place(meta)  # writable() took the x bits off
    foreign = archive.disc("foreign")
    res = _run(archive, tmp_path, [*archive.discs.values(), foreign], interactive="off",
               meta=meta)
    assert res.returncode == 0, res.stderr[-3000:]
    assert f"The disc labelled {foreign.name} belongs to a" in res.stderr
    assert "Several backup sets" not in res.stderr
    assert set(archive.sets[DOC].manifest.values()) <= _shas(tmp_path / "out")
