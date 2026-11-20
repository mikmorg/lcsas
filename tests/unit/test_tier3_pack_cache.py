"""#602: tier 3's pack cache asks for each data disc about once.

Without a cache the Python reader asks for a disc each time it needs a
pack that is not on the disc in the drive: on an archive whose files
alternate between discs, about once per file.  With
``LCSAS_PACK_CACHE_DIR`` (tier 1's variable) it copies the packs of this
repository on each disc it finds into the cache, so each data disc is
needed once.

The swap is scripted: one "drive" folder holds one disc at a time, and
the answer to each disc prompt puts in the disc that holds the pack the
prompt names.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from collections import namedtuple
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback as fb
from lcsas.restore.standalone_builder import build_standalone

from .tier3_repo_builder import PASSWORD, build_repo

N_DISCS = 5
N_FILES = 30


def _tree() -> dict[str, object]:
    """30 files in three folders, 10 to a folder."""
    return {
        f"dir{d}": {
            f"file{d}{i}.txt": f"content of file {d}.{i}\n".encode() * (i + 1)
            for i in range(10)
        }
        for d in range(3)
    }


class Archive:
    """A repository whose packs are spread over N_DISCS discs, and one
    drive that holds one disc at a time."""

    def __init__(self, tmp_path: Path) -> None:
        self.tree = _tree()
        self.repo = build_repo(tmp_path, self.tree, pack_per_blob=True)
        self.discs_dir = tmp_path / "discs"
        self.drive = tmp_path / "drive"
        self.drive.mkdir()
        self.in_drive: int | None = None
        self.disc_of: dict[str, int] = {}
        # Round-robin in pack-id order (fixed by the content, so the
        # same every run): neighbouring files are mostly on different
        # discs, the worst case for a reader with no cache.
        packs = sorted(
            (p for p in (self.repo / "data").rglob("*") if p.is_file()),
            key=lambda p: p.name,
        )
        assert len(packs) > N_FILES  # one pack per blob, trees included
        for n in range(N_DISCS):
            disc = self.discs_dir / f"disc{n}"
            (disc / "data").mkdir(parents=True)
            (disc / "volume_info.json").write_text(
                json.dumps({"label": f"LCSAS-T-{n:04d}", "type": "data"})
            )
        for i, pack in enumerate(packs):
            n = i % N_DISCS
            dst = self.discs_dir / f"disc{n}" / "data" / pack.parent.name
            dst.mkdir(exist_ok=True)
            shutil.move(str(pack), str(dst / pack.name))
            self.disc_of[pack.name] = n

    def insert(self, n: int) -> None:
        """Take the disc in the drive out and put disc *n* in."""
        if self.in_drive == n:
            return
        if self.in_drive is not None:
            os.rename(self.drive, self.discs_dir / f"disc{self.in_drive}")
        else:
            self.drive.rmdir()
        os.rename(self.discs_dir / f"disc{n}", self.drive)
        self.in_drive = n


def _restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cache: Path | None,
) -> tuple[Archive, list[str], Path]:
    """Restore the archive with a scripted disc swap; return the
    archive, the pack named by each prompt, and the target."""
    arc = Archive(tmp_path)
    prompts: list[str] = []

    def prompt_out(text: str) -> None:
        """The prompt names the first 16 hex digits of the pack."""
        for m in re.finditer(r"\| Pack ([0-9a-f]{16})\.\.\. is required", text):
            (pack_id,) = (p for p in arc.disc_of if p.startswith(m[1]))
            prompts.append(pack_id)

    def prompt_in() -> str:
        """ENTER after putting in the disc the prompt asked for."""
        arc.insert(arc.disc_of[prompts[-1]])
        return "\n"

    restorer = fb.PurePythonRestorer(
        arc.repo, password=PASSWORD,
        pack_search_paths=[arc.repo / "data", arc.drive],
        interactive=True, pack_cache_dir=cache,
        prompt_in=prompt_in, prompt_out=prompt_out,
    )
    target = tmp_path / "out"
    restorer.restore(target)
    assert restorer.failures == 0
    for d, files in arc.tree.items():
        assert isinstance(files, dict)
        for name, data in files.items():
            assert (target / d / name).read_bytes() == data
    return arc, prompts, target


def test_with_the_cache_each_disc_is_asked_for_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = tmp_path / "cache"
    _arc, prompts, _target = _restore(tmp_path, monkeypatch, cache)
    assert len(prompts) <= N_DISCS + 1, prompts
    # The cache holds the packs, in tier 1's layout, and no part files.
    cached = [p for p in (cache / "data").rglob("*") if p.is_file()]
    assert cached
    assert all(p.parent.name == p.name[:2] for p in cached)
    assert not [p for p in cached if p.name.endswith(".part")]
    for p in cached:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == p.name


def test_without_the_cache_discs_are_asked_for_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The comparison that makes the test above mean something."""
    _arc, prompts, _target = _restore(tmp_path, monkeypatch, None)
    assert len(prompts) > 3 * N_DISCS, prompts


def test_a_disc_too_big_for_the_drive_is_not_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Tier 1's free-space rule: no copy that would leave less than the
    margin free.  The restore still completes, from the discs."""
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        shutil, "disk_usage", lambda _p: usage(10**12, 10**12 - 1000, 1000)
    )
    cache = tmp_path / "cache"
    _arc, prompts, _target = _restore(tmp_path, monkeypatch, cache)
    assert not [p for p in cache.rglob("*") if p.is_file()]
    assert len(prompts) > N_DISCS
    err = capsys.readouterr().err
    assert err.count("too little to copy") == 1


def test_only_this_repositorys_packs_are_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A disc holds the packs of several repositories; only the packs
    this repository's index names are copied."""
    repo = build_repo(tmp_path, {"a.txt": b"alpha\n"})
    disc = tmp_path / "disc"
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    other = "cd" * 32
    (disc / "data" / "cd").mkdir()
    (disc / "data" / "cd" / other).write_bytes(b"another repository's pack")
    cache = tmp_path / "cache"
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", disc],
        interactive=False, pack_cache_dir=cache,
    )
    restorer.restore(tmp_path / "out")
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"alpha\n"
    names = {p.name for p in (cache / "data").rglob("*") if p.is_file()}
    assert names and other not in names
    # The second restore reads from the cache, with the disc gone.
    shutil.rmtree(disc)
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", disc],
        interactive=False, pack_cache_dir=cache, resume=False,
    )
    restorer.restore(tmp_path / "out2")
    assert (tmp_path / "out2" / "a.txt").read_bytes() == b"alpha\n"


def _run_script(
    tmp_path: Path, env_cache: str | None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    repo = build_repo(tmp_path, {"a.txt": b"alpha\n", "b": {"c.txt": b"c\n"}})
    disc = tmp_path / "disc"
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    pw = tmp_path / "pw"
    pw.write_bytes(PASSWORD + b"\n")
    script = tmp_path / "python_reader.py"
    script.write_text(build_standalone())
    env = {k: v for k, v in os.environ.items() if k != "LCSAS_PACK_CACHE_DIR"}
    if env_cache is not None:
        env["LCSAS_PACK_CACHE_DIR"] = env_cache
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--repo", str(repo),
         "--password-file", str(pw), "--restore-folder", str(tmp_path / "out"),
         "--mount-point", str(disc), "--interactive", "off"],
        capture_output=True, text=True, timeout=120, env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out" / "b" / "c.txt").read_bytes() == b"c\n"
    return proc, disc


def test_the_script_reads_lcsas_pack_cache_dir(tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    proc, _disc = _run_script(tmp_path, str(cache))
    assert [p for p in (cache / "data").rglob("*") if p.is_file()]
    assert "to the cache" in proc.stderr


@pytest.mark.parametrize("value", ["", None])
def test_an_empty_or_unset_cache_variable_turns_the_cache_off(
    tmp_path: Path, value: str | None,
) -> None:
    proc, _disc = _run_script(tmp_path, value)
    assert "to the cache" not in proc.stderr
    assert not (tmp_path / "cache").exists()


def test_a_cache_that_cannot_be_made_is_turned_off(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    blocker = tmp_path / "file"
    blocker.write_bytes(b"")
    repo = build_repo(tmp_path, {"a.txt": b"alpha\n"})
    # The packs are on a disc, which a working cache would copy.
    disc = tmp_path / "disc"
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", disc],
        interactive=False, pack_cache_dir=blocker / "cache",
    )
    assert "Cannot create the pack cache" in capsys.readouterr().err
    restorer.restore(tmp_path / "out")
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"alpha\n"
    # The cache is off: nothing was copied (DiscSearch's own check of
    # this is test_disc_search.py::test_a_cache_that_cannot_be_made_is_off).
    assert "to the cache" not in capsys.readouterr().err
