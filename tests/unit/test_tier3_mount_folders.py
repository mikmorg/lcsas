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

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback as fb
from lcsas.restore.standalone_builder import build_standalone

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
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=True,
    )
    prompts: list[str] = []

    class Reader:
        def readline(self) -> str:
            os.rename(tmp_path / "shelf" / "LCSAS-0002", mounts / "LCSAS-0002")
            return "\n"

    monkeypatch.setattr(restorer, "_print_swap_prompt", prompts.append)
    monkeypatch.setattr(sys, "stdin", Reader())
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    assert len(prompts) == 1
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

    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", mounts],
        interactive=False, meta_disc=meta,
    )
    assert restorer._scan_pack_search_paths(pack) is None
    assert meta not in restorer._all_roots()
    # The meta disc came out and a data disc went in at the same place
    # (no restore.sh there): it is searched.
    sentinel.unlink()
    assert restorer._scan_pack_search_paths(pack) is not None


def test_a_meta_disc_passed_as_a_root_is_skipped_while_mounted(
    tmp_path: Path,
) -> None:
    mounts = tmp_path / "media" / "user"
    meta = _meta(mounts)
    restorer = fb.PurePythonRestorer(
        build_repo(tmp_path, _TREE), password=PASSWORD,
        pack_search_paths=[meta], interactive=False, meta_disc=meta,
    )
    assert restorer._live_roots() == []


def test_disc_roots_and_pack_folders_are_not_listed(tmp_path: Path) -> None:
    """Only mount folders are listed: not a root that is a disc (it has
    data/), not the repository's data folder, not the cache."""
    mounts = tmp_path / "media"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    disc = mounts / "LCSAS-0001"
    (disc / "metadata").mkdir()
    (repo / "data" / "ab").mkdir()
    cache = tmp_path / "cache"
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD,
        pack_search_paths=[repo / "data", disc, mounts, tmp_path],
        interactive=False, pack_cache_dir=cache,
    )
    restorer._refresh_discovered()
    found = {p.name for p in restorer._discovered}
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


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_copy_is_passed_over(tmp_path: Path) -> None:
    """rc10 sweep, tier1-missing: a folder below the mount folder that
    only root may read held the packs too.  Tier 3 took that copy, could
    not open it, and failed every file.  A copy this user cannot read is
    now not a copy: the readable disc is used, as tier 1 does."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    # An unreadable copy of every pack, listed first ("A" < "L").
    blocked = mounts / "A-root-only"
    shutil.copytree(mounts / "LCSAS-0001" / "data", blocked / "data")
    for p in (blocked / "data").rglob("*"):
        if p.is_file():
            p.chmod(0)
    try:
        restorer = fb.PurePythonRestorer(
            repo, password=PASSWORD,
            pack_search_paths=[repo / "data", mounts], interactive=False,
        )
        restorer.restore(tmp_path / "out")
        assert restorer.failures == 0
        _check(tmp_path / "out")
    finally:
        for p in (blocked / "data").rglob("*"):
            if p.is_file():
                p.chmod(0o600)


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root enters any folder")
def test_a_folder_that_cannot_be_entered_is_passed_over(tmp_path: Path) -> None:
    """rc10 sweep, third run of tier1-missing: a folder below the mount
    folder could not be entered (mode 000 for this user).  pathlib's
    is_file() raised PermissionError instead of saying False, so the
    search itself stopped and every file failed.  Such a folder is now
    passed over and the readable disc is used."""
    mounts = tmp_path / "media" / "user"
    repo = _repo_and_disc(tmp_path, mounts, "LCSAS-0001")
    blocked = mounts / "A-shut"
    shutil.copytree(mounts / "LCSAS-0001" / "data", blocked / "data")
    blocked.chmod(0)
    try:
        restorer = fb.PurePythonRestorer(
            repo, password=PASSWORD,
            pack_search_paths=[repo / "data", mounts], interactive=False,
        )
        restorer.restore(tmp_path / "out")
        assert restorer.failures == 0
        _check(tmp_path / "out")
    finally:
        blocked.chmod(0o700)
