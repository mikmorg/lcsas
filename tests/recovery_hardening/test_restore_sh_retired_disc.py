"""restore.sh and a disc whose backup sets were all retired (#992).

An archive (``tests/contract/journeys.py``'s ``build_sessions``, archive
"a retired set's disc": real rustic, the owner's own commands): the set
photos staged and burned on the first disc, then retired; documents
staged and burned on the second disc; the META disc built from the
catalog.  Neither the second disc nor the META disc carries photos'
metadata; both name it in metadata/RETIRED_SETS.txt.

Up to rc11 the first disc met the #952 rule for a disc of another
archive ("belongs to a different LCSAS archive"), against #959's own
sentence, which says to use that disc as the newest data disc.

Run through a pipe, not relocated (so ``make shell-coverage`` traces
them); the journey of the same name in ``tests/contract/journeys.py``
runs it under a terminal through both scripts.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.contract.journeys import SESSIONS, Archive, build_sessions, writable

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic,
              pytest.mark.requires_xorriso]

RETIRED = "photos"
FOREIGN = "different LCSAS archive"


def _words(text: str) -> str:
    return " ".join(text.split())


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory):
    work = tmp_path_factory.mktemp("retired")
    built = build_sessions(SESSIONS["a retired set's disc"], work)
    _runnable_in_place(built.meta)
    yield built
    for d in (built.meta, *built.discs.values()):
        writable(d)


def _runnable_in_place(meta: Path) -> None:
    """The META disc's programs executable, as on a mounted disc (the
    archive's folders drop every x bit; these runs do not relocate)."""
    for f in (meta / "recovery" / "bin").rglob("*"):
        if f.is_file():
            f.chmod(0o555)


def _meta_copy(archive: Archive, tmp: Path) -> Path:
    """A writable copy of the META disc, to change its retired-set list."""
    meta = tmp / "meta" / "LCSAS_META"
    shutil.copytree(archive.meta, meta, symlinks=True)
    writable(meta)
    _runnable_in_place(meta)  # writable() took the x bits off
    return meta


def _run(archive: Archive, tmp: Path, discs: list[Path], name: str = RETIRED,
         meta: Path | None = None) -> subprocess.CompletedProcess[str]:
    mnt = tmp / "mnt"
    mnt.mkdir()
    for d in discs:
        os.symlink(d, mnt / d.name)
    for sub in ("home", "tmp"):
        (tmp / sub).mkdir()
    pw = tmp / "pw.txt"
    pw.write_text(archive.sets[name].password)
    env = {
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
        "LCSAS_BACKUP_SET": name,
    }
    return subprocess.run(
        ["sh", str((meta or archive.meta) / "restore.sh"), "--password-file", str(pw),
         "--restore-folder", str(tmp / "out")],
        capture_output=True, text=True, env=env, timeout=300,
        stdin=subprocess.DEVNULL,
    )


def _restored(archive: Archive, tmp: Path, name: str = RETIRED) -> bool:
    got = {hashlib.sha256(p.read_bytes()).hexdigest()
           for p in (tmp / "out").rglob("*") if p.is_file()}
    return set(archive.sets[name].manifest.values()) <= got


def _sentence(label: str) -> re.Pattern[str]:
    return re.compile(
        rf"{RETIRED}: retired after \d{{4}}-\d\d-\d\d\. Its backups are on the "
        rf"discs up to {label}\. To restore it, use that disc as the "
        r'"newest data disc"\.')


def test_the_retired_sets_disc_alone_is_used_and_named(
        archive: Archive, tmp_path: Path) -> None:
    oldest = archive.disc("oldest")
    res = _run(archive, tmp_path, [oldest])
    assert res.returncode == 0, res.stderr[-3000:]
    assert FOREIGN not in _words(res.stderr), res.stderr[-3000:]
    assert _sentence(oldest.name).search(_words(res.stderr)), res.stderr[-3000:]
    # Said once, though the disc is in reach and the set is chosen.
    assert res.stderr.count("A retired backup set:") == 1
    assert _restored(archive, tmp_path)
    assert not (tmp_path / "tools").exists()


def test_with_no_list_on_any_disc_it_is_refused_as_before(
        archive: Archive, tmp_path: Path) -> None:
    """A META disc made before the list existed, and the first disc,
    whose list names no set: the disc is another archive's, as up to
    rc11 (#952)."""
    meta = _meta_copy(archive, tmp_path)
    (meta / "metadata" / "RETIRED_SETS.txt").unlink()
    oldest = archive.disc("oldest")
    res = _run(archive, tmp_path, [oldest], meta=meta)
    assert res.returncode == 1, res.stderr[-3000:]
    assert FOREIGN in _words(res.stderr)
    assert "not among the available backup sets" in res.stderr
    assert "A retired backup set:" not in res.stderr


def test_with_no_list_on_the_meta_disc_the_newest_data_discs_is_taken(
        archive: Archive, tmp_path: Path) -> None:
    meta = _meta_copy(archive, tmp_path)
    (meta / "metadata" / "RETIRED_SETS.txt").unlink()
    oldest = archive.disc("oldest")
    res = _run(archive, tmp_path, list(archive.discs.values()), meta=meta)
    assert res.returncode == 0, res.stderr[-3000:]
    assert FOREIGN not in _words(res.stderr), res.stderr[-3000:]
    assert _sentence(oldest.name).search(_words(res.stderr)), res.stderr[-3000:]
    assert _restored(archive, tmp_path)
    # .lcsas-retired went with the tools folder.
    assert not (tmp_path / "tools").exists()


def test_a_retired_set_with_no_disc_is_named_so(
        archive: Archive, tmp_path: Path) -> None:
    """A line with "-" for the disc: none of its files is on a disc
    (RetiredSet.sentence() without a label)."""
    meta = _meta_copy(archive, tmp_path)
    listing = meta / "metadata" / "RETIRED_SETS.txt"
    set_id = archive.sets[RETIRED].repo_id
    listing.write_text("# a comment line\n"
                       f"{set_id}|-|-|{RETIRED}\r\n")
    res = _run(archive, tmp_path, [archive.disc("oldest")], meta=meta)
    assert res.returncode == 0, res.stderr[-3000:]
    assert (f"{RETIRED}: retired. None of its backups is on a disc."
            in _words(res.stderr)), res.stderr[-3000:]
    assert _restored(archive, tmp_path)


def test_the_live_set_is_restored_as_before(archive: Archive, tmp_path: Path) -> None:
    """The newest disc, the live set: no word of the retired one."""
    res = _run(archive, tmp_path, [archive.disc("newest")], name="documents")
    assert res.returncode == 0, res.stderr[-3000:]
    assert "A retired backup set:" not in res.stderr
    assert _restored(archive, tmp_path, "documents")
