"""restore.sh and an heir's imperfect inputs (#966).

On a real archive (``tests/contract/journeys.py``'s ``build_sessions``:
real rustic, two burn sessions, the owner's own commands), restore.sh
run through a pipe, as ``make shell-coverage`` traces it:

- a restore folder that cannot be written is refused before any
  question, in words (it failed after the password, with a resume
  command that failed the same way); a quoted ``~/...`` is the home
  folder, not a folder named ``~``;
- ``--list-snapshots`` lists the set's snapshots with their dates and
  restores nothing;
- ``--snapshot`` naming no snapshot of the set says so, and leaves no
  resume.sh (it carried the same id and failed again);
- the password's temporary file is not left in $TMPDIR, and one a
  killed run left there is removed.

The questions themselves (another set, the set menu) are journeys in
``tests/contract/journeys.py``, through both scripts.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

from tests.contract.journeys import SESSIONS, Archive, build_sessions, writable

from .test_restore_sh_older_disc import runnable_in_place

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic,
              pytest.mark.requires_xorriso]

DOC = "documents"


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory):
    built = build_sessions(SESSIONS["two sessions"], tmp_path_factory.mktemp("sessions"))
    runnable_in_place(built.meta)
    yield built
    for d in (built.meta, *built.discs.values()):
        writable(d)


def _run(archive: Archive, tmp: Path, *args: str,
         env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    mnt = tmp / "mnt"
    mnt.mkdir(exist_ok=True)
    for d in archive.discs.values():
        if not (mnt / d.name).exists():
            os.symlink(d, mnt / d.name)
    for sub in ("home", "tmp"):
        (tmp / sub).mkdir(exist_ok=True)
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
        "LCSAS_INTERACTIVE": "off",
        "LCSAS_ALLOW_TMPFS_RESTORE_FOLDER": "1",
        "LCSAS_PASSWORD": archive.sets[DOC].password,
        **(env or {}),
    }
    return subprocess.run(["sh", str(archive.meta / "restore.sh"), *args],
                          capture_output=True, text=True, env=full,
                          stdin=subprocess.DEVNULL, timeout=300)


def _shas(folder: Path) -> set[str]:
    return {hashlib.sha256(p.read_bytes()).hexdigest()
            for p in folder.rglob("*") if p.is_file()}


def test_a_read_only_restore_folder_is_refused_before_any_question(
        archive: Archive, tmp_path: Path) -> None:
    folder = tmp_path / "ro"
    folder.mkdir()
    folder.chmod(0o555)
    try:
        res = _run(archive, tmp_path, "--restore-folder", str(folder))
    finally:
        folder.chmod(0o755)
    assert res.returncode == 2, res.stderr[-3000:]
    assert f"cannot restore into {folder}:" in res.stderr
    assert "does not let you write in" in res.stderr
    assert "checking the password" not in res.stderr
    assert not (folder / ".lcsas-restore-marker").exists()


def test_a_restore_folder_under_a_file_is_refused(archive: Archive, tmp_path: Path) -> None:
    (tmp_path / "afile").write_text("x")
    res = _run(archive, tmp_path, "--restore-folder", str(tmp_path / "afile" / "restored"))
    assert res.returncode == 2, res.stderr[-3000:]
    assert f"{tmp_path / 'afile'} is a file, not a folder" in res.stderr


def test_a_quoted_tilde_is_the_home_folder(archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, "--restore-folder", "~/restored")
    assert res.returncode == 0, res.stderr[-3000:]
    assert set(archive.sets[DOC].manifest.values()) <= _shas(tmp_path / "home" / "restored")
    assert not list(tmp_path.rglob("~"))
    # The end says where the files are and how many.
    assert f"Restore complete.  2 files restored to:\n    {tmp_path / 'home' / 'restored'}" \
        in res.stderr


def test_list_snapshots_lists_with_dates_and_restores_nothing(
        archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, "--list-snapshots")
    assert res.returncode == 0, res.stderr[-3000:]
    assert "The snapshots of documents" in res.stderr
    rows = [ln for ln in res.stdout.splitlines()[1:] if ln.strip()]
    assert len(rows) == 2, res.stdout
    for row in rows:
        assert row[8:10] == "  " and row[10:12] == "20", row
    assert not (tmp_path / "home" / "restored").exists()
    assert not list((tmp_path / "tmp").glob("lcsas-list.*"))
    # Stopped at the password: its stand-in folder goes all the same.
    res = _run(archive, tmp_path, "--list-snapshots", env={"LCSAS_PASSWORD": ""})
    assert res.returncode == 77, res.stderr[-3000:]
    assert not list((tmp_path / "tmp").glob("lcsas-list.*"))


def test_snapshot_of_no_snapshot_leaves_no_resume(archive: Archive, tmp_path: Path) -> None:
    res = _run(archive, tmp_path, "--snapshot", "abcdef12",
               "--restore-folder", str(tmp_path / "out"))
    assert res.returncode == 1, res.stderr[-3000:]
    assert "No snapshot of documents starts with abcdef12." in res.stderr
    assert "sh restore.sh --list-snapshots" in res.stderr
    assert not (tmp_path / "tools" / "resume.sh").exists()
    assert "resume.sh" not in res.stderr


def test_the_password_file_is_not_left_in_tmpdir(archive: Archive, tmp_path: Path) -> None:
    stale = tmp_path / "tmp" / "lcsas-pw.Stale1"
    stale.parent.mkdir(parents=True)
    stale.write_text("an old password\n")
    res = _run(archive, tmp_path, "--restore-folder", str(tmp_path / "out"))
    assert res.returncode == 0, res.stderr[-3000:]
    assert not list((tmp_path / "tmp").glob("lcsas-pw.*"))
