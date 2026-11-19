"""The disc search's fault matrix (#885): every probe fails in turn.

The rc10 sweep failed four times on one kind of bug: a probe of the
Python reader's disc search raised where it should have said "no"
(#877, #880, #881, #882).  Since #885 every filesystem touch of the
search goes through ``_DiscFs``, whose methods answer and never raise
``OSError``.  This matrix holds it to that, through a whole restore.

A small repository is restored by the real ``PurePythonRestorer`` with
the disc search on an in-memory tree (``FakeDiscFs``).  Under the mount
folder sit a sibling folder holding a disc catalog and a copy of every
pack but one, and the meta disc.  The data disc, with every pack, goes
in at the first disc prompt.  Then, for each ``_DiscFs`` method and
each class of path, that method fails on that path with EACCES, EIO
and ENOENT in turn, with and without a pack cache.  Every cell must:

* restore every file with no failure when a readable copy exists;
* otherwise stop at the disc prompt (the reader types ``q``), never on a
  traceback and never with "FAILED (continuing)" for a search fault;
* ask for a disc at most 1 + (number of discs) times.

``TOUCHES`` says which method meets which path class, so a cell whose
fault is never met is known to be one and cannot pass by accident.  The
four rc10 cases are cells of the matrix, named by their test ids.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from lcsas.restore import restic_fallback as fb
from lcsas.restore._disc_search import (
    EXIT_READER_QUIT,
    DiscSearch,
    RestoreAbortedError,
    _DiscFs,
)

from .disc_fs_fake import METHODS, SCRATCH, FakeDiscFs
from .tier3_repo_builder import PASSWORD, build_repo

_TREE = {
    "a.txt": b"alpha\n",
    "d": {"b.txt": b"bravo\n", "c.txt": b"charlie\n"},
}
LABEL = "LCSAS-0001"
N_DISCS = 1

MOUNT = "/v/media/user"            # the mount folder (a search root)
SIBLING = f"{MOUNT}/A-sibling"     # listed before the disc ("A" < "L")
CATALOG = f"{SIBLING}/catalog.db"
META = f"{MOUNT}/LCSAS-META"
MARKER = f"{META}/recovery/scripts/restore.sh"
SHELF = f"/v/shelf/{LABEL}"        # the disc, before it goes in
DISC = f"{MOUNT}/{LABEL}"          # the disc, once in
CACHE = "/v/cache"


@dataclass(frozen=True)
class Archive:
    repo: Path
    packs: dict[str, bytes]        # pack id -> bytes
    only_on_disc: str              # the pack the sibling has no copy of
    catalog: bytes


def _catalog(path: Path, packs: list[str]) -> bytes:
    """A disc catalog listing every pack on the one disc."""
    db = sqlite3.connect(path)
    db.executescript(
        "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY, label TEXT);"
        "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT);"
        "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);"
    )
    db.execute("INSERT INTO volumes VALUES (1, ?)", (LABEL,))
    for i, p in enumerate(packs, 1):
        db.execute("INSERT INTO packs VALUES (?, ?)", (i, p))
        db.execute("INSERT INTO volume_packs VALUES (1, ?)", (i,))
    db.commit()
    db.close()
    return path.read_bytes()


@pytest.fixture(scope="module")
def archive(tmp_path_factory: pytest.TempPathFactory) -> Archive:
    """The repository, built once: its packs are kept in memory and its
    own ``data/`` is left empty (the restorer reads the rest of it from
    the real filesystem; the disc search reads only the fake)."""
    base = tmp_path_factory.mktemp("faults")
    repo = build_repo(base, _TREE, pack_per_blob=True)
    packs = {
        p.name: p.read_bytes()
        for p in (repo / "data").rglob("*") if p.is_file()
    }
    assert len(packs) > 2
    # The pack ids change with each build (the encryption is random), so
    # the pack only the disc has is chosen by the order the restore
    # reads them: the last one.  The copies found first then serve the
    # restore until the disc is asked for, in every build.
    order: list[str] = []

    class Recording(_DiscFs):
        def _read_range(self, path: Path, offset: int, length: int) -> bytes:
            if path.name not in order:
                order.append(path.name)
            return super()._read_range(path, offset, length)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(fb, "DiscSearch", functools.partial(DiscSearch, fs=Recording()))
        restorer = fb.PurePythonRestorer(
            repo, password=PASSWORD, interactive=False, resume=False,
        )
        restorer.restore(base / "order")
    assert restorer.failures == 0 and sorted(order) == sorted(packs)
    shutil.rmtree(repo / "data")
    (repo / "data").mkdir()
    ids = sorted(packs)
    return Archive(repo, packs, order[-1], _catalog(base / "catalog.db", ids))


def _fake(arc: Archive, tmp_path: Path) -> FakeDiscFs:
    fake = FakeDiscFs(tmp_path)
    fake.add_dir(arc.repo / "data")
    fake.add_dir(MOUNT)
    fake.add_file(CATALOG, arc.catalog)
    fake.add_file(MARKER, b"#!/bin/sh\n")
    fake.add_file(f"{SHELF}/volume_info.json", b'{"label": "%s"}' % LABEL.encode())
    for pid, data in arc.packs.items():
        fake.add_file(f"{SHELF}/data/{pid[:2]}/{pid}", data)
        if pid != arc.only_on_disc:
            fake.add_file(f"{SIBLING}/data/{pid[:2]}/{pid}", data)
    return fake


def _first_copies(arc: Archive) -> list[tuple[str, bool]]:
    return [
        (f"{SIBLING}/data/{pid[:2]}/{pid}", False)
        for pid in arc.packs if pid != arc.only_on_disc
    ]


#: Path class -> the faulted paths, each (path, and everything below it?).
CLASSES = {
    # A folder under the mount folder that cannot be entered (#881).
    "sibling": lambda arc: [(SIBLING, True)],
    # The mount folder itself (listed for discs), not what is in it.
    "mount": lambda arc: [(MOUNT, False)],
    # The pack copies the search comes to first (#877, #880).
    "first_copy": _first_copies,
    # The whole pack cache.
    "cache": lambda arc: [(CACHE, True)],
    # The disc catalog on a search root, read at the prompt (#882).
    "catalog": lambda arc: [(CATALOG, False)],
    # The meta disc's marker: is it still mounted?
    "meta_marker": lambda arc: [(MARKER, False)],
    # The folder the prompt keeps its catalog copies in (#635).
    "catalog_copies": lambda arc: [(SCRATCH, True)],
}

ERRNOS = {"EACCES": errno.EACCES, "EIO": errno.EIO, "ENOENT": errno.ENOENT}


@dataclass
class Outcome:
    failures: int
    aborted: RestoreAbortedError | None
    prompts: int
    hits: list[tuple[str, str]]
    log: str
    target: Path


def _restore(
    arc: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str], faults: list[tuple[str, str, int, bool]],
    *, cache: bool,
) -> Outcome:
    """Restore *arc* with *faults* (method, path, errno, below) set; the
    disc goes in at the first prompt, and any later prompt is answered
    'q'."""
    fake = _fake(arc, tmp_path)
    for method, path, err, below in faults:
        fake.fail(method, path, err, below=below)
    monkeypatch.setattr(fb, "DiscSearch", functools.partial(DiscSearch, fs=fake))
    shown: list[str] = []
    answers: list[str] = []

    def prompt_in() -> str:
        answers.append("")
        if len(answers) == 1:
            fake.move(SHELF, DISC)
            return "\n"
        return "q\n"

    restorer = fb.PurePythonRestorer(
        arc.repo, password=PASSWORD,
        pack_search_paths=[arc.repo / "data", Path(MOUNT)],
        interactive=True, pack_cache_dir=Path(CACHE) if cache else None,
        meta_disc=Path(META), prompt_in=prompt_in, prompt_out=shown.append,
        resume=False,
    )
    target = tmp_path / "out"
    aborted = None
    try:
        restorer.restore(target)
    except RestoreAbortedError as exc:
        aborted = exc
    finally:
        # What a caller does when done: remove the catalog copies.
        restorer._search.close()
    text = "".join(shown)
    return Outcome(
        failures=restorer.failures, aborted=aborted,
        prompts=text.count("is required for the next file"),
        hits=fake.hits, log=capsys.readouterr().err + text, target=target,
    )


def _restored(target: Path) -> bool:
    return (
        (target / "a.txt").read_bytes() == b"alpha\n"
        and (target / "d" / "b.txt").read_bytes() == b"bravo\n"
        and (target / "d" / "c.txt").read_bytes() == b"charlie\n"
    )


# ── What each cell must do ───────────────────────────────────────

#: Cells with no readable copy left: the search must end at the prompt.
#: Only one: the mount folder cannot be listed, so the disc that goes in
#: below it is never seen (tier 1 behaves the same).
NO_COPY = {("listdir", "mount")}

#: Which path classes each method meets in a restore, with and without
#: a pack cache.  A cell outside this table has a fault that is never
#: met; the cell checks that, so this table cannot rot unnoticed.
#: The two differ where only the pack cache looks: the cache folder,
#: and the copying of a disc's packs into it (which lists and stats the
#: sibling's copies, and reads them through copy_checked rather than
#: read_range).
_BOTH: dict[str, set[str]] = {
    # The pack search (candidates, the cache copy), the meta disc's
    # marker and the catalog search at the prompt.
    "is_file": {"sibling", "first_copy", "catalog", "meta_marker"},
    # Below the mount folder: is a name there a folder (a disc)?
    "is_dir": {"sibling"},
    # The mount folder, listed for the discs mounted below it.
    "listdir": {"mount"},
    # The mode check on a candidate copy (#877).
    "readable": {"sibling", "first_copy"},
    # Catalog paths, made canonical so each is read once.
    "realpath": {"sibling", "catalog"},
    # Paths compared by their text (the meta disc, the roots).
    "abspath": {"mount", "sibling", "first_copy"},
    # The catalog read at the prompt, through a connection, and the
    # local copies of it (#635) made in a scratch folder.
    "open_catalog": {"sibling", "catalog", "catalog_copies"},
    "scratch_dir": {"catalog_copies"},
    "write_file": {"catalog_copies"},
    "remove_tree": {"catalog_copies"},
}
TOUCHES: dict[bool, dict[str, set[str]]] = {
    False: {
        **_BOTH,
        # A pack read from the copy found first, and the catalog read
        # to be copied.
        "read_range": {"sibling", "first_copy", "catalog"},
    },
    True: {
        **_BOTH,
        "is_file": _BOTH["is_file"] | {"cache"},
        # The sibling's data folder, listed for its packs to copy.
        "listdir": _BOTH["listdir"] | {"sibling"},
        # The cache, and the folders compared with it, by their links
        # and by their text.
        "realpath": _BOTH["realpath"] | {"cache"},
        "abspath": _BOTH["abspath"] | {"cache"},
        # Is a pack in the cache already; each pack's size.
        "stat": {"cache", "sibling", "first_copy"},
        # Packs are read from the cache; the copies found first are read
        # by copy_checked instead.
        "read_range": {"cache", "sibling", "catalog"},
        "copy_checked": {"cache", "sibling", "first_copy"},
        "free_bytes": {"cache"},
        "tree_bytes": {"cache"},
        "make_dirs": {"cache"},
    },
}


def _cells() -> list[object]:
    named = {
        # rc10, first run of tier1-missing (#877): a copy this user may
        # not read, listed first; it must be passed over.
        ("readable", "first_copy", "EACCES", False):
            "rc10-877-an_unreadable_copy_is_passed_over",
        # rc10, second run (#880): the mode check said yes, open() said
        # EACCES; the copy is set aside and another is found.
        ("read_range", "first_copy", "EACCES", False):
            "rc10-880-a_copy_that_fails_to_open_is_set_aside",
        # rc10, third run (#881): a folder below the mount folder that
        # cannot be entered; is_file() raised in the pack search.
        ("is_file", "sibling", "EACCES", False):
            "rc10-881-a_folder_that_cannot_be_entered_is_passed_over",
        # rc10, fourth run (#882): the same, for the catalog the disc
        # prompt looks for on a search root.
        ("is_file", "catalog", "EACCES", False):
            "rc10-882-the_disc_prompt_passes_over_a_folder_that_cannot_be_entered",
    }
    cells: list[object] = []
    for cache in (False, True):
        for method in METHODS:
            for cls in CLASSES:
                for name in ERRNOS:
                    key = (method, cls, name, cache)
                    cid = named.get(
                        key,
                        f"{method}-{cls}-{name}-{'cache' if cache else 'nocache'}",
                    )
                    cells.append(pytest.param(method, cls, name, cache, id=cid))
    return cells


@pytest.mark.parametrize(("method", "cls", "err_name", "cache"), _cells())
def test_a_failing_probe_never_stops_the_search(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    method: str, cls: str, err_name: str, cache: bool,
) -> None:
    faults = [
        (method, path, ERRNOS[err_name], below)
        for path, below in CLASSES[cls](archive)
    ]
    out = _restore(archive, tmp_path, monkeypatch, capsys, faults, cache=cache)

    assert "FAILED (continuing)" not in out.log, out.log
    assert "Traceback" not in out.log, out.log
    assert out.prompts <= 1 + N_DISCS, out.log
    if (method, cls) in NO_COPY:
        assert out.aborted is not None, out.log
        assert out.aborted.exit_code == EXIT_READER_QUIT
        assert out.prompts >= 1
    else:
        assert out.aborted is None, out.log
        assert out.failures == 0, out.log
        assert _restored(out.target)

    if (method, cls, err_name, cache) == ("read_range", "first_copy", "EACCES", False):
        # #880: the copy that would not open is named, once each.
        assert "looking for another copy." in out.log, out.log
    met = bool(out.hits)
    assert met == (cls in TOUCHES[cache].get(method, set())), (
        f"fault met: {met}; hits: {sorted(set(out.hits))[:5]}"
    )


def test_the_matrix_names_every_method_of_the_seam() -> None:
    """A method added to _DiscFs must join the matrix."""
    public = {
        n for n, v in vars(_DiscFs).items()
        if callable(v) and not n.startswith("_")
    }
    assert public == set(METHODS)


def test_no_fault_restores_with_one_prompt(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The matrix's baseline: no fault, every file restored, and the
    disc asked for once, by the label the catalog gives."""
    for cache in (False, True):
        run = tmp_path / str(cache)
        run.mkdir()
        out = _restore(archive, run, monkeypatch, capsys, [], cache=cache)
        assert out.failures == 0 and out.aborted is None, out.log
        assert _restored(out.target)
        assert out.prompts == 1, out.log
        assert LABEL in out.log
        monkeypatch.undo()


def test_the_cache_holds_the_packs_after_a_restore(
    archive: Archive, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With a cache, the search really reads from it (so the cache
    cells of the matrix test something)."""
    fake = _fake(archive, tmp_path)
    monkeypatch.setattr(fb, "DiscSearch", functools.partial(DiscSearch, fs=fake))
    ids = sorted(archive.packs)
    restorer = fb.PurePythonRestorer(
        archive.repo, password=PASSWORD,
        pack_search_paths=[archive.repo / "data", Path(MOUNT)],
        interactive=True, pack_cache_dir=Path(CACHE),
        prompt_in=lambda: (fake.move(SHELF, DISC), "\n")[1],
        prompt_out=lambda _t: None, resume=False,
    )
    restorer.restore(tmp_path / "out")
    assert restorer.failures == 0
    cached = {
        k.rsplit("/", 1)[1] for k in fake.files if k.startswith(CACHE + "/")
    }
    assert cached == set(ids)
    assert all(
        hashlib.sha256(fake.files[f"{CACHE}/data/{p[:2]}/{p}"]).hexdigest() == p
        for p in ids
    )
