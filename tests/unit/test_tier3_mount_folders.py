"""Tier 3 finds a data disc mounted one level below a mount folder.

Desktops mount a disc at ``/media/<user>/<LABEL>``,
``/run/media/<user>/<LABEL>`` or ``/Volumes/<LABEL>``: a new folder for
each disc, named by its label.  restore.sh hands tier 3 the mount
folders (``--mount-point /media/<user>``), and tier 3 searched only
those folders and their ``data/``, so it never found such a disc.  Tier
1 lists each mount folder one level deep (disc_locator.c,
refresh_discovered), and now tier 3 does too.  While the meta disc is
mounted, nothing on it is used (tier 1: meta_disc_is_live, path_under).
"""

from __future__ import annotations

import errno
import functools
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback as fb
from lcsas.restore._disc_search import DiscSearch, RestoreAbortedError
from lcsas.restore.standalone_builder import build_standalone

from .disc_fs_fake import FakeDiscFs
from .tier3_repo_builder import PASSWORD, build_repo

_TREE = {"a.txt": b"alpha\n", "d": {"b.txt": b"bravo\n"}}


def _repo_and_disc(tmp_path: Path, mount_dir: Path, label: str) -> Path:
    """A repository with no data/, its packs on a disc mounted at
    ``mount_dir/label``.  Returns the repository."""
    repo = build_repo(tmp_path, _TREE)
    disc = mount_dir / label
    disc.parent.mkdir(parents=True, exist_ok=True)
    disc.mkdir()
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    (disc / "volume_info.json").write_text(json.dumps({"label": label}))
    return repo


def _check(target: Path) -> None:
    assert (target / "a.txt").read_bytes() == b"alpha\n"
    assert (target / "d" / "b.txt").read_bytes() == b"bravo\n"


def _on_fake_fs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeDiscFs:
    """From here on the restorer's disc search sees *tmp_path* (as it
    is now) through an in-memory tree, where a test can make a path
    fail without chmod, and so without depending on not being root
    (#885).  The rest of the restorer still reads the real files."""
    sqlite_dir = tmp_path / "fake-sqlite"
    sqlite_dir.mkdir()
    fake = FakeDiscFs(sqlite_dir)
    fake.mirror(tmp_path)
    monkeypatch.setattr(fb, "DiscSearch", functools.partial(DiscSearch, fs=fake))
    return fake


def _search(repo: Path, roots: list[Path], **kw: object) -> DiscSearch:
    """The disc search a restorer of *repo* over *roots* would use."""
    return DiscSearch(
        roots, repo_data=repo / "data", log=lambda _msg: None,
        **kw,  # type: ignore[arg-type]
    )


def _searched(repo: Path, roots: list[Path], **kw: object) -> list[str]:
    """The folders the disc prompt of such a search says it is
    searching (the roots, then the discs found below them), as it prints
    them: a long path is cut from the left, so each keeps its last name.
    The prompt is answered 'q'."""
    out: list[str] = []
    search = _search(repo, roots, interactive=True,
                     prompt_in=lambda: "q\n", prompt_out=out.append, **kw)
    with pytest.raises(RestoreAbortedError):
        search.read_pack_range("ff" * 32, 0, 1)
    box = "".join(out).split("\n")
    start = box.index("| Currently searching:".ljust(59) + "|") + 1
    end = box.index("|" + " " * 58 + "|", start)
    return [line.strip("| ").rstrip() for line in box[start:end]]


@pytest.mark.parametrize(
    "layout", ["media/user", "run/media/user", "Volumes"],
)
def test_a_disc_one_level_below_a_mount_folder_is_found(
    tmp_path: Path, layout: str,
) -> None:
    mounts = tmp_path / layout
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    _check(tmp_path / "out")


def test_two_levels_down_is_not_searched(tmp_path: Path) -> None:
    """Tier 1's depth: the mount folder and its folders, no deeper."""
    repo = _repo_and_disc(tmp_path, tmp_path / "media" / "user", "LCSAS-0001")
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", tmp_path / "media"],
        interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures > 0


def test_a_disc_mounted_at_the_prompt_under_a_new_name_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The disc is mounted only after the prompt, in a folder that did
    not exist when the restore started."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, tmp_path / "shelf", "LCSAS-0002")
    mounts.mkdir(parents=True)
    prompts: list[str] = []

    def enter() -> str:
        os.rename(tmp_path / "shelf" / "LCSAS-0002", mounts / "LCSAS-0002")
        return "\n"

    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=True, prompt_in=enter, prompt_out=prompts.append,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    assert "".join(prompts).count("is required for the next file") == 1
    _check(tmp_path / "out")


def _meta(mounts: Path) -> Path:
    meta = mounts / "LCSAS-META"
    (meta / "recovery" / "scripts").mkdir(parents=True)
    (meta / "recovery" / "scripts" / "restore.sh").write_text("#!/bin/sh\n")
    return meta


def test_nothing_on_the_mounted_meta_disc_is_used(tmp_path: Path) -> None:
    """A pack that is (only) under the meta disc's mount point is not
    used while the meta disc is mounted, as in tier 1, so the reader
    never holds the meta disc."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-META")
    meta = mounts / "LCSAS-META"
    (meta / "recovery" / "scripts").mkdir(parents=True)
    sentinel = meta / "recovery" / "scripts" / "restore.sh"
    sentinel.write_text("#!/bin/sh\n")
    pack = next(p for p in (meta / "data").rglob("*") if p.is_file()).name

    search = _search(repo, [repo / "data", mounts], meta_disc=meta)
    with pytest.raises(FileNotFoundError):
        search.read_pack_range(pack, 0, 1)
    assert not [f for f in _searched(repo, [repo / "data", mounts],
                                     meta_disc=meta)
                if f.endswith("LCSAS-META")]
    # The meta disc came out and a data disc went in at the same place
    # (no restore.sh there): it is searched.
    sentinel.unlink()
    first = next(p for p in (meta / "data").rglob("*") if p.is_file())
    assert search.read_pack_range(pack, 0, 4) == first.read_bytes()[:4]


def test_a_meta_disc_passed_as_a_root_is_skipped_while_mounted(
    tmp_path: Path,
) -> None:
    mounts = tmp_path / "media" / "user"
    meta = _meta(mounts)
    assert _searched(build_repo(tmp_path, _TREE), [meta], meta_disc=meta) == []


def test_disc_roots_and_pack_folders_are_not_listed(tmp_path: Path) -> None:
    """Only mount folders are listed: not a root that is a disc (it has
    data/), not the repository's data folder, not the cache."""
    mounts = tmp_path / "media"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    disc = mounts / "LCSAS-0001"
    (disc / "metadata").mkdir()
    (repo / "data" / "ab").mkdir()
    cache = tmp_path / "cache"
    # The prompt lists the roots, then the discs found below them.
    names = [f.rsplit("/", 1)[-1] for f in _searched(
        repo, [repo / "data", disc, mounts, tmp_path], pack_cache=cache,
    )]
    found = names[4:]
    assert names[:4] == ["data", "LCSAS-0001", "media", tmp_path.name]
    assert found, names                     # tmp_path's folders are listed
    assert "metadata" not in found          # disc root not listed
    assert "ab" not in found                # repo data not listed
    assert "LCSAS-0001" not in found        # already a root
    assert "cache" not in found
    assert "media" not in found             # already a root


def test_the_script_finds_a_disc_under_a_mount_folder(tmp_path: Path) -> None:
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    pw = tmp_path / "pw"
    pw.write_bytes(PASSWORD + b"\n")
    script = tmp_path / "standalone_restorer.py"
    script.write_text(build_standalone())
    env = {k: v for k, v in os.environ.items()
           if k not in ("LCSAS_PACK_CACHE_DIR", "LCSAS_META_DISC")}
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--repo", str(repo),
         "--password-file", str(pw), "--target", str(tmp_path / "out"),
         "--mount-point", str(mounts), "--interactive", "off",
         "--meta-disc", str(mounts / "LCSAS-META")],
        capture_output=True, text=True, timeout=120, env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    _check(tmp_path / "out")


def test_an_unreadable_copy_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc10 sweep, tier1-missing (#877): a folder below the mount folder
    that only root may read held the packs too.  Tier 3 took that copy,
    could not open it, and failed every file.  A copy this user cannot
    read is now not a copy: the readable disc is used, as tier 1 does.
    (Was chmod 0 on each copy; now the in-memory tree, #885.)"""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    # An unreadable copy of every pack, listed first ("A" < "L").
    blocked = mounts / "A-root-only"
    shutil.copytree(mounts / "LCSAS-0001" / "data", blocked / "data")
    fake = _on_fake_fs(tmp_path, monkeypatch)
    copies = [p for p in (blocked / "data").rglob("*") if p.is_file()]
    for p in copies:
        # Mode 000: it can be seen, not read.
        for method in ("readable", "read_range", "copy_checked"):
            fake.fail(method, p, errno.EACCES)
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", mounts], interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    _check(tmp_path / "out")
    assert any(m == "readable" for m, _p in fake.hits), fake.hits


def test_a_hidden_folder_below_a_mount_folder_is_not_searched(
    tmp_path: Path,
) -> None:
    """A mounted disc is never a hidden folder; skip them all."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, ".hidden-disc")
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", mounts], interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures > 0


def test_a_copy_that_fails_to_open_is_set_aside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc10 sweep re-run, tier1-missing: a mode check said a copy was
    readable but open() was refused (EACCES).  The copy is set aside for
    the run and the readable disc is used instead of failing the file."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    blocked = mounts / "A-refused"
    shutil.copytree(mounts / "LCSAS-0001" / "data", blocked / "data")
    real_open = open

    def refusing_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(blocked) in str(file):
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr("builtins.open", refusing_open)
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", mounts], interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    monkeypatch.undo()
    _check(tmp_path / "out")


def test_a_folder_that_cannot_be_entered_is_passed_over(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """rc10 sweep, third run of tier1-missing (#881): a folder below the
    mount folder could not be entered (mode 000 for this user).
    pathlib's is_file() raised PermissionError instead of saying False,
    so the search itself stopped and every file failed.  Such a folder
    is now passed over and the readable disc is used.  (Was chmod 0 on
    the folder; now the in-memory tree, #885.)"""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    blocked = mounts / "A-shut"
    shutil.copytree(mounts / "LCSAS-0001" / "data", blocked / "data")
    fake = _on_fake_fs(tmp_path, monkeypatch)
    fake.shut(blocked)
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", mounts], interactive=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    _check(tmp_path / "out")
    assert any(m == "is_file" for m, _p in fake.hits), fake.hits


def test_the_disc_prompt_passes_over_a_folder_that_cannot_be_entered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """rc10 sweep, fourth run of tier1-missing (#882): at the disc
    prompt tier 3 looks for disc catalogs below the mount folder, and an
    unenterable folder there (holding a catalog.db) raised
    PermissionError, so every file failed.  The real prompt runs here
    (it is not patched out), and the disc mounted at it is used.  (Was
    chmod 0 on the folder; now the in-memory tree, #885.  The test below
    still does it on the real filesystem.)"""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, tmp_path / "shelf", "LCSAS-0002")
    mounts.mkdir(parents=True)
    blocked = mounts / "A-shut"
    (blocked / "data").mkdir(parents=True)
    (blocked / "catalog.db").write_bytes(b"not a catalog")
    fake = _on_fake_fs(tmp_path, monkeypatch)
    fake.shut(blocked)

    def enter() -> str:
        shelf = tmp_path / "shelf" / "LCSAS-0002"
        if fake.key(shelf) in fake.dirs:
            fake.move(shelf, mounts / "LCSAS-0002")
        return "\n"

    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=True, prompt_in=enter,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0, capsys.readouterr().err
    _check(tmp_path / "out")
    assert ("is_file", fake.key(blocked / "catalog.db")) in fake.hits


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_a_folder_that_cannot_be_entered_on_the_real_filesystem(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The real-filesystem smoke test of the cases above (#885): a real
    mode-000 folder below the mount folder, holding a catalog.db, met by
    the pack search and by the real disc prompt, with the disc mounted
    at the prompt.  (The rc10 test of #882, kept on chmod.)"""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, tmp_path / "shelf", "LCSAS-0002")
    mounts.mkdir(parents=True)
    blocked = mounts / "A-shut"
    (blocked / "data").mkdir(parents=True)
    (blocked / "catalog.db").write_bytes(b"not a catalog")
    blocked.chmod(0)

    def enter() -> str:
        shelf = tmp_path / "shelf" / "LCSAS-0002"
        if shelf.exists():
            os.rename(shelf, mounts / "LCSAS-0002")
        return "\n"

    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=True, prompt_in=enter,
    )
    try:
        restorer.restore(tmp_path / "out")
    finally:
        blocked.chmod(0o700)
    assert restorer.failures == 0, capsys.readouterr().err
    _check(tmp_path / "out")
