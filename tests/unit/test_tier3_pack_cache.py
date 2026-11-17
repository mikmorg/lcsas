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

import collections
import errno
import hashlib
import json
import os
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
    restorer = fb.PurePythonRestorer(
        arc.repo, password=PASSWORD,
        pack_search_paths=[arc.repo / "data", arc.drive],
        interactive=True, pack_cache_dir=cache,
    )
    prompts: list[str] = []

    def prompt(pack_id: str) -> None:
        prompts.append(pack_id)

    class Reader:
        """stdin: ENTER after putting in the disc the prompt asked for."""

        def readline(self) -> str:
            arc.insert(arc.disc_of[prompts[-1]])
            return "\n"

    monkeypatch.setattr(restorer, "_print_swap_prompt", prompt)
    monkeypatch.setattr(sys, "stdin", Reader())
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
        fb.shutil, "disk_usage", lambda _p: usage(10**12, 10**12 - 1000, 1000)
    )
    cache = tmp_path / "cache"
    _arc, prompts, _target = _restore(tmp_path, monkeypatch, cache)
    assert not [p for p in cache.rglob("*") if p.is_file()]
    assert len(prompts) > N_DISCS
    err = capsys.readouterr().err
    assert err.count("too little to copy") == 1


def test_drain_fits_keeps_a_margin() -> None:
    gib = 1 << 30
    assert fb._drain_fits(free=10 * gib, total=100 * gib, need=8 * gib)
    assert not fb._drain_fits(free=10 * gib, total=100 * gib, need=9 * gib + 1)
    # 5% of a small drive.
    assert fb._drain_fits(free=200, total=1000, need=150)
    assert not fb._drain_fits(free=200, total=1000, need=151)
    assert not fb._drain_fits(free=100, total=1000, need=101)


def test_a_damaged_pack_is_not_kept_in_the_cache(tmp_path: Path) -> None:
    src = tmp_path / "pack"
    src.write_bytes(b"not what the id says")
    dst = tmp_path / "cache" / "data" / "ab" / ("ab" * 32)
    assert fb._copy_pack(src, dst, "ab" * 32, lambda _n: None) is False
    assert not dst.exists()
    assert not dst.with_name(dst.name + ".part").exists()
    good = hashlib.sha256(src.read_bytes()).hexdigest()
    dst = tmp_path / "cache" / "data" / good[:2] / good
    assert fb._copy_pack(src, dst, good, lambda _n: None) is True
    assert dst.read_bytes() == src.read_bytes()


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
    script = tmp_path / "standalone_restorer.py"
    script.write_text(build_standalone())
    env = {k: v for k, v in os.environ.items() if k != "LCSAS_PACK_CACHE_DIR"}
    if env_cache is not None:
        env["LCSAS_PACK_CACHE_DIR"] = env_cache
    proc = subprocess.run(
        [sys.executable, "-I", str(script), "--repo", str(repo),
         "--password-file", str(pw), "--target", str(tmp_path / "out"),
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
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, interactive=False,
        pack_cache_dir=blocker / "cache",
    )
    assert restorer._pack_cache is None
    assert "Cannot create the pack cache" in capsys.readouterr().err
    restorer.restore(tmp_path / "out")
    assert (tmp_path / "out" / "a.txt").read_bytes() == b"alpha\n"


def test_a_pack_that_cannot_be_copied_is_tried_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """One unreadable pack (EIO) and one with bytes that do not match
    its id: each is tried once, the other packs are still copied, and
    the restore completes from the disc."""
    repo = build_repo(
        tmp_path, {f"f{i}.txt": f"file {i}\n".encode() for i in range(6)},
        pack_per_blob=True,
    )
    disc = tmp_path / "disc"
    shutil.move(str(repo / "data"), str(disc / "data"))
    (repo / "data").mkdir()
    packs = sorted(p for p in (disc / "data").rglob("*") if p.is_file())
    unreadable, padded = packs[0].name, packs[1]
    # Bytes after the last blob: every blob still reads, the hash does not.
    with open(padded, "ab") as f:
        f.write(b"\0")

    tries: collections.Counter[str] = collections.Counter()
    real_copy = fb._copy_pack

    def copy(src: Path, dst: Path, pack_id: str, tick: object) -> bool:
        tries[pack_id] += 1
        if pack_id == unreadable:
            raise OSError(errno.EIO, "Input/output error")
        return real_copy(src, dst, pack_id, tick)  # type: ignore[arg-type]

    monkeypatch.setattr(fb, "_copy_pack", copy)
    cache = tmp_path / "cache"
    restorer = fb.PurePythonRestorer(
        repo, password=PASSWORD, pack_search_paths=[repo / "data", disc],
        interactive=False, pack_cache_dir=cache,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    for i in range(6):
        assert (tmp_path / "out" / f"f{i}.txt").read_bytes() == f"file {i}\n".encode()
    assert tries[unreadable] == 1
    assert tries[padded.name] == 1
    cached = {p.name for p in (cache / "data").rglob("*") if p.is_file()}
    assert cached == {p.name for p in packs} - {unreadable, padded.name}
    err = capsys.readouterr().err
    assert err.count("does not match its id") == 1
    assert err.count("Input/output error") == 1
