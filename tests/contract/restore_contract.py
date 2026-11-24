"""The restore contract (#886): one table of cases, run on every reader.

What a restore does when X happens is implemented twice: in tier 1 (C,
``lcsas-restore``) and in the Python reader (tier 3, the generated
``python_reader.py``).  ``restore.sh`` falls from one to the other,
so an heir must get the same answer from both.  This module states that
answer once, as data, and drives the real readers on it.

- A :class:`Case` is data: the tree to back up, what goes wrong
  (:class:`Faults`) and the :class:`Outcome` every reader must produce.
- :func:`run_tier1` and :func:`run_tier3` build the repository, run the
  real reader and return the :class:`Outcome` it actually produced: its
  exit code, an ``lstat`` walk of what it restored, the rows of its
  ``RESTORE_FAILURES.txt`` and the last lines it printed.
- :func:`mismatches` compares an outcome with the expected one, and
  :func:`disagreements` two readers' outcomes with each other.

The test is ``tests/recovery_hardening/test_contract_readers.py`` (in
the ``recovery-hardening`` CI job, which has rustic and builds tier 1).

A known difference between the readers is never hidden: a case that
has one names it in ``differs`` (reader -> the outcome that reader gives
today) with the reason in ``why``, and the test that the readers agree
is then an expected failure (strict, so it goes red once they agree).

Repositories are built with real ``rustic``; a tree rustic cannot write
(a device without root, a name that will not decode, a node with no
type) is written directly by ``tests/unit/tier3_repo_builder.py``.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import random
import re
import shutil
import socket
import stat
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from tests.contract.disc_catalog import (
    NO_CATALOG,
    NO_RECORD,
    NONE_LISTED,
    Vol,
    named_discs,
    newer_than,
    write_catalog,
)
from tests.contract.exit_codes import ReaderExit
from tests.recovery_hardening import test_name_escaping_differential as _esc
from tests.recovery_hardening._diff_helpers import (
    build_rustic_repo,
    find_restore_bin,
    latest_snapshot_id,
    rustic_backup,
)

READERS = ("tier1", "tier3")
FAILURES_NAME = "RESTORE_FAILURES.txt"
PASSWORD = b"test-password"


# ── What is backed up and what is restored ──────────────────────────


@dataclass(frozen=True)
class Entry:
    """One item of a tree: what is backed up, or what a reader restored.

    ``data`` is a regular file's content (its SHA-256 when longer than
    64 bytes), a link's target, a device's ``st_rdev``; ``mode`` is the
    permission bits (``S_IMODE``), ``None`` for a link.  In an expected
    tree, ``None`` in ``data`` or ``mode`` means "any".
    """

    kind: Literal["file", "dir", "link", "fifo", "socket", "chardev",
                  "blockdev", "other"]
    data: bytes | None = None
    mode: int | None = None

    def matches(self, got: Entry) -> bool:
        return (self.kind == got.kind
                and (self.data is None or self.data == got.data)
                and (self.mode is None or self.mode == got.mode))

    def __repr__(self) -> str:
        mode = "" if self.mode is None else f" {self.mode:04o}"
        data = "" if self.data is None else f" {self.data!r}"
        return f"<{self.kind}{mode}{data}>"


def _content(data: bytes) -> bytes:
    return data if len(data) <= 64 else hashlib.sha256(data).hexdigest().encode()


def File(content: bytes = b"", mode: int | None = 0o644) -> Entry:  # noqa: N802
    """A regular file; its content is written to the source as given."""
    return Entry("file", content, mode)


def Dir(mode: int | None = 0o755) -> Entry:  # noqa: N802
    """A folder (parents of a path are made as folders too)."""
    return Entry("dir", None, mode)


def Link(target: bytes) -> Entry:  # noqa: N802
    """A symlink.  ``@OUTSIDE@`` in the target is the folder outside the
    restore target that a case checks nothing is written to."""
    return Entry("link", target, None)


def Fifo(mode: int | None = 0o644) -> Entry:  # noqa: N802
    return Entry("fifo", None, mode)


def Socket() -> Entry:  # noqa: N802
    """A Unix socket (bound in the source, so Linux paths only)."""
    return Entry("socket", None, None)


#: A tree to back up with rustic: relative byte path -> entry.
Tree = Mapping[bytes, Entry]

#: Not compared: what a reader leaves behind is not part of the case.
ANY = "any"


@dataclass(frozen=True)
class DiscCat:
    """A data disc in the mount folder that holds only its
    ``catalog.db`` (#950): which catalog a reader takes is the case."""

    #: Its folder in the mount folder.
    name: str
    #: The catalog's volumes, as for ``Faults.catalog``.
    vols: tuple[Vol, ...]
    #: Its label mark (``PRAGMA user_version``).
    mark: int
    #: How many seconds older than the newest the file's time is.
    age: int = 0
    #: False: the catalog has no record of the missing packs.
    knows_pack: bool = True
    #: Another archive's disc: its packs belong to another backup set.
    foreign: bool = False
    #: Cut the file to this many bytes.
    cut_to: int | None = None
    #: Also a data disc (#993): a ``volume_info.json`` with its label,
    #: and a ``data/`` folder holding a copy of one pack of the
    #: repository that is there (not a missing one).
    data: bool = False


@dataclass(frozen=True)
class Faults:
    """What goes wrong (or differs from a plain restore) in a case."""

    #: The password file's bytes.
    password_file: bytes = PASSWORD + b"\n"
    #: ``--snapshot`` for the judged run (#967: "latest" and an empty
    #: id); ``None``: left off, so the newest snapshot is restored.
    snapshot: str | None = None
    #: A first run into the same target before the one judged:
    #: "before" restores the snapshot of ``Case.before``; "latest" the
    #: same snapshot (the judged run is then a resume).  It must exit 0.
    first_run: Literal["before", "latest"] | None = None
    #: Paths whose data packs are not there for the judged run ("*":
    #: every pack that holds only data blobs).  In a builder repository
    #: (one pack per blob) a path takes away the packs of the item's own
    #: blobs (a folder: its tree blob), and ``path#N`` only the pack of
    #: a file's N-th content blob (from 0).
    missing: tuple[bytes, ...] = ()
    #: Paths (one data blob each, in a pack of its own) whose pack is
    #: cut short in the middle of that blob for the judged run.
    truncated: tuple[bytes, ...] = ()
    #: Paths (likewise) whose pack has one byte flipped in the middle of
    #: that blob, so its MAC fails, for the judged run.
    corrupted: tuple[bytes, ...] = ()
    #: Paths (likewise) whose pack is there but mode 000 (cannot be
    #: opened by a user who is not root) for the judged run.
    unreadable: tuple[bytes, ...] = ()
    #: Run with the disc prompt on, and this on stdin.
    answers: bytes | None = None
    #: The target is a 1 MiB tmpfs (needs passwordless ``sudo mount``).
    target_full: bool = False
    #: Leave ``--restore-folder`` off the command line.
    usage_error: bool = False
    #: Repository files (relative paths) emptied before the judged run:
    #: a folder loses what is in it, a file is removed.
    damage: tuple[str, ...] = ()
    #: A disc catalog passed with ``--catalog``: the volumes (volume_id
    #: 1, 2, ... in order) that hold every missing pack (#896).
    catalog: tuple[Vol, ...] | None = None
    #: False: the catalog has those volumes but no record of the packs.
    catalog_knows_pack: bool = True
    #: Discs in a mount folder the readers search, each holding only a
    #: catalog (#950).
    discs: tuple[DiscCat, ...] = ()
    #: The disc whose catalog is passed with ``--catalog``, as the
    #: scripts pass the one with the newest file time.
    catalog_from: str | None = None
    #: #993: the backup set id the catalogs record, when the ``--repo``
    #: folder is not named by it (``None``: the folder's name).
    set_id: str | None = None
    #: Run with ``LCSAS_PACK_CACHE_DIR`` set (a folder in the run's own
    #: work folder).
    pack_cache: bool = False


@dataclass(frozen=True)
class Outcome:
    """What a reader did.  Expected outcomes pin only what they state.

    ``restored``: ``None`` = everything backed up, less the paths in
    ``failures`` (and what is below them); ``ANY`` = not compared;
    otherwise the exact tree under the restored root.
    ``failures``: rows of ``RESTORE_FAILURES.txt``, (path relative to
    the restored root, reason).  An expected reason is a prefix.
    ``summary``: a regex searched in the last lines the reader printed.
    ``wrote_outside``: something outside the target changed.
    ``discs``: what the first disc prompt names (``named_discs``): the
    labels in order, or ``NONE_LISTED`` / ``NO_RECORD`` / ``NO_CATALOG``
    / ``NO_PROMPT``; ``None`` = not compared.
    ``says``: regexes searched in everything the reader printed;
    ``never_says``: regexes that must not be found there.
    """

    exit: int
    restored: Mapping[bytes, Entry] | str | None = None
    failures: tuple[tuple[str, str], ...] = ()
    summary: str = ""
    wrote_outside: bool = False
    discs: tuple[str, ...] | str | None = None
    says: tuple[str, ...] = ()
    never_says: tuple[str, ...] = ()
    #: Only on an outcome a driver returns: the reader's output.
    output: str = field(default="", compare=False, repr=False)


#: A tree written straight into repository files by
#: ``tests/unit/tier3_repo_builder.py`` (names as rustic stores them).
BuilderTree = dict


@dataclass(frozen=True)
class Case:
    name: str
    tree: Tree | BuilderTree
    expect: Outcome
    faults: Faults = Faults()
    #: The tree of an earlier backup of the same folder (a snapshot of
    #: its own, before the one restored).
    before: Tree | None = None
    #: "rustic" (real rustic backs up ``tree``) or "builder".
    repo: Literal["rustic", "builder"] = "rustic"
    #: Builder only: each blob in a pack of its own.
    pack_per_blob: bool = False
    #: Run only as root ("root"), or only when not root ("user").
    needs: Literal["root", "user"] | None = None
    #: Known differences: reader -> the outcome it gives today.
    differs: Mapping[str, Outcome] = field(default_factory=dict)
    why: str = ""
    #: Which older test this case replaces, or "new".
    replaces: str = "new"

    def expected(self, reader: str) -> Outcome:
        return self.differs.get(reader, self.expect)


# ── The cases ────────────────────────────────────────────────────────

_DONE = r"restore complete|Restore complete"
_NOT_ALL = r"finished with errors|could NOT be restored"
#: RESTORE_FAILURES.txt reasons (#909: the same text from both readers;
#: the 64-hex pack id follows).
_PACK_NOT_FOUND = "pack not found: "
_PACK_DAMAGED = "pack damaged: "
_PACK_UNREADABLE = "pack unreadable: "

# #781: one name of each class rustic stores escaped (quote, backslash,
# control characters, bytes that are not UTF-8), and a folder and a
# link with such names: the names of test_name_escaping_differential.py
# (which also runs them through tier 2).
_NAMES_TREE: dict[bytes, Entry] = {n: File(n) for n in _esc.NAMES}
_NAMES_TREE[_esc.DIR_NAME + b"/" + _esc.INNER] = File(_esc.INNER)
_NAMES_TREE[_esc.LINK_NAME] = Link(_esc.LINK_TARGET)

_MISSING_PACK_BEFORE: dict[bytes, Entry] = {b"a": File(b"alpha\n")}
_MISSING_PACK_TREE: dict[bytes, Entry] = {
    b"a": File(b"alpha\n"), b"b": File(b"beta: its pack is missing\n"),
}

# #896: which discs the prompt names, by tier 1's rule (TIERS.txt,
# "WHICH DISCS THE PROMPT NAMES"): every volume holding the pack that
# is not DESTROYED, newest volume (highest volume number) first.  Here
# that is neither label order nor oldest-first (a BD25 volume
# consolidated onto a later BD50 one), so a reader that sorts by label,
# or oldest first, disagrees.
_V1 = "LCSAS_BD25_2026_0001"
_V2 = "LCSAS_BD50_2026_0002"


def _disc_prompt(name: str, vols: tuple[Vol, ...],
                 named: tuple[str, ...] | str, knows: bool = True) -> Case:
    """The pack of ``b`` is on no disc; the catalog puts it on *vols*.
    The restored tree is not compared: tier 1 leaves an empty ``b``
    (see "disc missing, q at the prompt"), and a ``differs`` for it
    would make the agreement on the discs named an expected failure."""
    return Case(
        name=f"disc prompt: {name}",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"q\n", catalog=vols,
                      catalog_knows_pack=knows),
        expect=Outcome(exit=ReaderExit.READER_QUIT, restored=ANY,
                       discs=named),
        replaces="new (#896)",
    )


_T1 = "LCSAS_TEST_TINY_2026_0001"
_T2 = "LCSAS_TEST_TINY_2026_0002"
_T3 = "LCSAS_TEST_TINY_2026_0003"
_OTHER = "LCSAS_TEST_TINY_2026_0009"
#: #993: the backup set id the catalogs record when the --repo folder
#: is named otherwise, and the line a reader prints for a catalog it
#: does not use because it is another archive's.
_SET_ID = "5e7a11d0"
_FOREIGN = r"another archive's catalog"


def _catalog_choice(name: str, discs: tuple[DiscCat, ...], passed: str,
                    named: tuple[str, ...] | str) -> Case:
    """#950: the pack of ``b`` is on no disc in reach; *discs* hold
    catalogs, and the one on *passed* is given with ``--catalog``."""
    return Case(
        name=f"catalog choice: {name}",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"q\n", discs=discs,
                      catalog_from=passed),
        expect=Outcome(exit=ReaderExit.READER_QUIT, restored=ANY,
                       discs=named),
        replaces="new (#950)",
    )


def _builder_special_tree() -> BuilderTree:
    from tests.unit.tier3_repo_builder import RawNode

    go_chardev = (1 << 26) | (1 << 21)   # os.ModeDevice | os.ModeCharDevice
    return {
        "good": b"good\n",
        "null": RawNode({"type": "chardev", "mode": go_chardev | 0o644,
                         "device": 259}),     # Linux makedev(1, 3)
    }


def _builder_refused_tree() -> BuilderTree:
    from tests.unit.tier3_repo_builder import RawNode

    return {
        "good": b"good\n",
        "..": b"dot dot\n",
        "bad\\q": b"an unknown escape\n",
        "notype": RawNode({"mode": 0o644}),
        "sub": {"x\\x": b"a short \\x escape\n", "ok": b"ok\n"},
    }


def _builder_spread_file_tree() -> BuilderTree:
    """A file of three content blobs, each blob in a pack of its own."""
    from tests.unit.tier3_repo_builder import Blob

    return {
        "a": b"alpha\n",
        "big": [Blob(b"big, part one\n"), Blob(b"big, part two\n"),
                Blob(b"big, part three\n")],
        "z": b"zed\n",
    }


def _builder_subfolder_tree() -> BuilderTree:
    return {
        "a": b"alpha\n",
        "sub": {"deep": {"y": b"y in sub/deep\n"}, "x": b"x in sub\n"},
        "z": b"zed\n",
    }


CASES: list[Case] = [
    Case(
        name="plain tree",
        tree={
            b"top.txt": File(b"top\n"),
            b"empty": File(b""),
            b"d/e/deep.txt": File(b"deep\n" * 100),
            b"d/x.sh": File(b"#!/bin/sh\n", 0o755),
            b"d/ro": File(b"read only\n", 0o444),
            b"emptydir": Dir(0o700),
        },
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="new (tier 1 alone: test_tier1_vs_tier2_differential.py)",
    ),
    Case(
        name="escaped names",
        tree=_NAMES_TREE,
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="test_name_escaping_differential.py (tiers 1 and 3)",
    ),
    Case(
        name="names refused and not decodable",
        repo="builder",
        tree=_builder_refused_tree(),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"good": File(b"good\n"), b"sub": Dir(),
                      b"sub/ok": File(b"ok\n")},
            failures=(
                ("..", "name refused: the name is '.' or '..'"),
                ("bad\\q", "name refused: the name cannot be decoded"),
                ("notype", "unknown item type: ?"),
                ("sub/x\\x", "name refused: the name cannot be decoded"),
            ),
            summary=_NOT_ALL,
        ),
        replaces="new (#872, #875: exit 4 in both)",
    ),
    Case(
        name="symlinks",
        tree={
            b"b/f": File(b"f\n"),
            b"a/up": Link(b"../b/f"),
            b"a/out": Link(b"../../../../outside"),
            b"abs": Link(b"/etc/hostname"),
            b"dangling": Link(b"nowhere"),
            b"nonutf": Link(b"tgt\xe9"),
        },
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="test_links_specials_differential.py::"
                 "test_links_and_specials_restore_alike (links, tiers 1, 3)",
    ),
    Case(
        name="fifo and socket",
        tree={b"fifo": Fifo(0o640), b"sock": Socket(), b"f": File(b"f\n")},
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="test_links_specials_differential.py::"
                 "test_links_and_specials_restore_alike (FIFO, socket)",
    ),
    Case(
        name="device without root",
        repo="builder",
        needs="user",
        tree=_builder_special_tree(),
        expect=Outcome(
            exit=ReaderExit.OK,
            restored={b"good": File(b"good\n")},
            failures=(("null", "special file (character device) not created"),),
            summary=_DONE,
        ),
        replaces="new (#783)",
    ),
    Case(
        name="restore over an earlier restore",
        before={
            b"d/x": Link(b"@OUTSIDE@/file"),      # A: link, B: file
            b"dd": Link(b"@OUTSIDE@/dir"),        # A: link, B: folder
            b"l/keep": File(b"keep\n"),           # A: folder, B: link
            b"q": Fifo(),                         # A: FIFO, B: file
        },
        tree={
            b"d/x": File(b"B\n"),
            b"dd/inner": File(b"inner\n"),
            b"l": Link(b"d/x"),
            b"q": File(b"q\n"),
        },
        faults=Faults(first_run="before"),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={
                b"d": Dir(None), b"d/x": File(b"B\n"),
                b"dd": Dir(None), b"dd/inner": File(b"inner\n"),
                b"l": Dir(None), b"l/keep": File(b"keep\n"),
                b"q": File(b"q\n"),
            },
            failures=(("l", "a folder with this name is already there"),),
            summary=_NOT_ALL,
            wrote_outside=False,
        ),
        replaces="test_links_specials_differential.py::"
                 "test_restore_over_earlier_restore_writes_nothing_outside "
                 "(tiers 1 and 3)",
    ),
    Case(
        name="setuid and setgid on own files",
        needs="user",
        tree={b"su": File(b"su\n", 0o4755), b"sg": File(b"sg\n", 0o2755),
              b"sticky": Dir(0o1777)},
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="new (#785, without root)",
    ),
    Case(
        name="setuid and setgid as root",
        needs="root",
        tree={b"su": File(b"su\n", 0o4755), b"sg": File(b"sg\n", 0o2755)},
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="test_links_specials_differential.py::"
                 "test_root_restore_keeps_owner_setuid_and_devices "
                 "(modes only; owner and device stay there)",
    ),
    Case(
        name="wrong password",
        tree={b"f": File(b"f\n")},
        faults=Faults(password_file=b"not-the-password\n"),
        expect=Outcome(exit=ReaderExit.WRONG_PASSWORD, restored={},
                       summary=r"wrong password"),
        replaces="new (#384, #805)",
    ),
    Case(
        name="no key files",
        tree={b"f": File(b"f\n")},
        faults=Faults(damage=("keys",)),
        expect=Outcome(exit=ReaderExit.FAILED, restored={}),
        replaces="new (#384: no key tested the password, so not 77)",
    ),
    Case(
        name="password file with two lines",
        tree={b"f": File(b"f\n")},
        faults=Faults(password_file=PASSWORD + b"\nsecond line\n"),
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="new (#641; tier 1 alone: test_tier1_password_file_rule.py)",
    ),
    Case(
        name="password file with CRLF",
        tree={b"f": File(b"f\n")},
        faults=Faults(password_file=PASSWORD + b"\r\n"),
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="new (#641; tier 1 alone: test_tier1_password_file_rule.py)",
    ),
    # #967 item 1: the documented default, typed out, restores the newest
    # of two snapshots in both readers.
    Case(
        name="--snapshot latest",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(snapshot="latest"),
        expect=Outcome(exit=ReaderExit.OK, summary=_DONE),
        replaces="new (#967)",
    ),
    # #967 item 1: an empty id is the start of every id.  With one
    # snapshot both readers restore it; with two it names none, and both
    # say so in tier 1's words.
    Case(
        name="--snapshot with an empty id, two snapshots",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(snapshot=""),
        expect=Outcome(exit=ReaderExit.FAILED, restored={},
                       summary=r"(?m)snapshot not found: ('')?$"),
        replaces="new (#967)",
    ),
    Case(
        name="target full",
        # Bytes zstd cannot shrink below the 1 MiB tmpfs.
        tree={b"big.bin": File(random.Random(886).randbytes(4 << 20))},
        faults=Faults(target_full=True),
        # #909: neither reader leaves the part-written file (or its
        # .lcsas-partial) behind: removing it gives the space back.
        expect=Outcome(exit=ReaderExit.DISK_FULL, restored={},
                       summary=r"is full"),
        replaces="new (tier 1 alone: test_tier1_target_full.py)",
    ),
    Case(
        name="disc missing, q at the prompt",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"q\n"),
        # #909: neither reader leaves the file it was writing (tier 1 left
        # it empty, mode 0600, before).  Both restore it on the next run.
        # #967: with no catalog the prompt says to put in the newest
        # data disc, in tier 1's words (tier 3 said "no catalog
        # available").
        expect=Outcome(
            exit=ReaderExit.READER_QUIT,
            restored={b"a": File(b"alpha\n")},
            summary=r"(?i)run the same command again|stopped",
            discs=NO_CATALOG,
        ),
        replaces="new (#658; test_restore_sh_driver.py::"
                 "test_tier1_done_and_quit_code_are_real, the 75)",
    ),
    # #949: the answer 's' ("I do not have this disc").  The discs the
    # box named are skipped for the rest of the run: the item is listed
    # as for a run with no prompt, the rest restored, exit 4, and the
    # end names the skipped discs.  One answer, so a second box would
    # read the end of input and stop with exit 75.
    Case(
        name="disc missing, s at the prompt",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"s\n",
                      catalog=(Vol(_V1), Vol(_V2))),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n")},
            failures=(("b", _PACK_NOT_FOUND),),
            summary=rf"Skipped discs: {_V2}, {_V1}\.",
            discs=(_V2, _V1),
        ),
        replaces="new (#949)",
    ),
    Case(
        name="two packs on skipped discs, one s",
        repo="builder", pack_per_blob=True,
        tree={"a": b"alpha\n", "b": b"beta\n", "c": b"gamma\n"},
        faults=Faults(missing=(b"b", b"c"), answers=b"s\n",
                      catalog=(Vol(_V1), Vol(_V2))),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n")},
            failures=(("b", _PACK_NOT_FOUND), ("c", _PACK_NOT_FOUND)),
            summary=r"2 item\(s\) need a disc that was not in a drive",
        ),
        replaces="new (#949)",
    ),
    Case(
        name="disc missing, s at a prompt that names no disc",
        repo="builder", pack_per_blob=True,
        tree={"a": b"alpha\n", "b": b"beta\n", "c": b"gamma\n"},
        faults=Faults(missing=(b"b", b"c"), answers=b"s\n"),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n")},
            failures=(("b", _PACK_NOT_FOUND), ("c", _PACK_NOT_FOUND)),
            summary=r"No disc was asked for after the answer 's'",
        ),
        replaces="new (#949)",
    ),
    Case(
        name="disc missing, no prompt",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",)),
        # #909: tier 1 stopped at the first pack it could not find
        # (exit 1, nothing listed, an empty file left).  Now both list
        # the file, restore the rest and exit 4.
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n")},
            failures=(("b", _PACK_NOT_FOUND),),
            summary=_NOT_ALL,
        ),
        replaces="new",
    ),
    # #909: data a reader needs is missing or unreadable and there is no
    # disc prompt.  These rows were pinned with each reader's outcome
    # before the tier-1 change (tier 1 stopped with exit 1); since it,
    # both readers list the item with the same reason, leave nothing at
    # its path, keep an older copy, restore the rest and exit 4.
    Case(
        name="file in several packs, a later one missing",
        repo="builder", pack_per_blob=True,
        tree=_builder_spread_file_tree(),
        faults=Faults(missing=(b"big#1",)),       # "part two" only
        # Nothing of big is left (tier 1 left part one, mode 0600).
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n"), b"z": File(b"zed\n")},
            failures=(("big", _PACK_NOT_FOUND),),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="the tree pack of a folder missing",
        repo="builder", pack_per_blob=True,
        tree=_builder_subfolder_tree(),
        faults=Faults(missing=(b"sub",)),         # sub's tree blob
        # The folder is made empty and listed (one row, none for what is
        # in it); z after it is restored.
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n"), b"sub": Dir(),
                      b"z": File(b"zed\n")},
            failures=(("sub", _PACK_NOT_FOUND),),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="every data pack missing",
        tree={b"a": File(b"alpha\n"), b"d/b": File(b"beta\n" * 50),
              b"empty": File(b"")},
        faults=Faults(missing=(b"*",)),
        # Each file with data is listed; the folder and the empty file
        # are restored.
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            failures=(("a", _PACK_NOT_FOUND), ("d/b", _PACK_NOT_FOUND)),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="older restore there, the newer pack missing",
        before={b"a": File(b"alpha\n"),
                b"b": File(b"b, as first backed up\n", 0o640)},
        tree={b"a": File(b"alpha\n"),
              b"b": File(b"b, as backed up later: its pack is missing\n")},
        faults=Faults(first_run="before", missing=(b"b",)),
        # The earlier file is kept as it was (tier 1 emptied it).
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            restored={b"a": File(b"alpha\n"),
                      b"b": File(b"b, as first backed up\n", 0o640)},
            failures=(("b", _PACK_NOT_FOUND),),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="pack cut short",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(truncated=(b"b",)),
        # "pack damaged": tier 3 cannot tell a short pack from a corrupt
        # one, so the row does not either (tier 1's own line still says
        # "pack truncated").
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            failures=(("b", _PACK_DAMAGED),),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="blob fails its MAC",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(corrupted=(b"b",)),
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            failures=(("b", _PACK_DAMAGED),),
            summary=_NOT_ALL,
        ),
    ),
    Case(
        name="pack there but unreadable",
        needs="user",              # root reads a mode-000 file
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(unreadable=(b"b",)),
        # Tier 1 finds the copy and cannot open it; tier 3 sets it aside
        # and finds no other: both say "pack unreadable", not "not found".
        expect=Outcome(
            exit=ReaderExit.NOT_ALL_RESTORED,
            failures=(("b", _PACK_UNREADABLE),),
            summary=_NOT_ALL,
        ),
        replaces="new (#909)",
    ),
    _disc_prompt("one volume", (Vol(_V1),), (_V1,)),
    _disc_prompt("two volumes, newest first", (Vol(_V1), Vol(_V2)),
                 (_V2, _V1)),
    _disc_prompt("one of two DESTROYED",
                 (Vol(_V1), Vol(_V2, "DESTROYED", "DESTROYED")), (_V1,)),
    _disc_prompt("one of two DEPRECATED",
                 (Vol(_V1, "DEPRECATED"), Vol(_V2)), (_V2, _V1)),
    _disc_prompt("one of two lost", (Vol(_V1, lost=True), Vol(_V2)),
                 (_V2, _V1)),
    _disc_prompt("a volume with no disc recorded", (Vol(_V1, disc=None),),
                 (_V1,)),
    _disc_prompt("its only volume DESTROYED",
                 (Vol(_V1, "DESTROYED", "DESTROYED"),), NONE_LISTED),
    # #957: the pack is on a disc burned after this catalog's.
    _disc_prompt("on a disc newer than the catalog",
                 (Vol(_V1, holds=False), Vol(_V2, holds=False),
                  Vol("LCSAS_BD50_2026_0003", "DESTROYED", "DESTROYED",
                      holds=False)),
                 newer_than(_V2)),
    _disc_prompt("pack unknown to the catalog", (Vol(_V1), Vol(_V2)),
                 NO_RECORD, knows=False),
    # #950: which catalog a reader takes, when several are in reach.
    # The scripts pass the one with the newest file time; tier 1 used
    # it, and never one older.  The pack is on a disc not in a drive.
    _catalog_choice(
        "the newest catalog is cut short, an older good one in reach",
        (DiscCat(_T2, (Vol(_T1), Vol(_T2, holds=False)), mark=2, age=100),
         DiscCat(_T3, (Vol(_T1), Vol(_T2, holds=False),
                       Vol(_T3, holds=False)), mark=3, cut_to=5000)),
        _T3, (_T1,)),
    _catalog_choice(
        "an older disc's catalog has the newest file time",
        (DiscCat(_T1, (Vol(_T1, holds=False),), mark=1,
                 knows_pack=False),
         DiscCat(_T3, (Vol(_T1, holds=False), Vol(_T2),
                       Vol(_T3, holds=False)), mark=3, age=100)),
        _T1, (_T2,)),
    _catalog_choice(
        "a disc of another archive has the newest catalog",
        (DiscCat(_OTHER, (Vol(_OTHER),), mark=9, foreign=True,
                 knows_pack=False),
         DiscCat(_T2, (Vol(_T1), Vol(_T2)), mark=2, age=100)),
        _OTHER, (_T2, _T1)),
    # #950: the disc the pack is newer than is named from this archive's
    # catalog, not from another archive's that lists more discs.
    _catalog_choice(
        "another archive's catalog lists more discs",
        (DiscCat(_OTHER, tuple(Vol(f"LCSAS_OTHER_2026_000{n}")
                               for n in range(1, 6)),
                 mark=9, foreign=True, knows_pack=False),
         DiscCat(_T2, (Vol(_T1, holds=False), Vol(_T2, holds=False)),
                 mark=2, age=100)),
        _OTHER, newer_than(_T2)),
    # #993: the --repo folder is not named by its backup set id (a
    # folder the heir named).  The catalogs know this repository's own
    # packs, so they are used: the prompt names the disc, and a data
    # disc is copied into the pack cache.
    Case(
        name="renamed repository folder: the prompt names the disc",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"q\n", set_id=_SET_ID,
                      discs=(DiscCat(_T2, (Vol(_T1), Vol(_T2)), mark=2),),
                      catalog_from=_T2),
        expect=Outcome(exit=ReaderExit.READER_QUIT, restored=ANY,
                       discs=(_T2, _T1), never_says=(_FOREIGN,)),
        replaces="new (#993)",
    ),
    Case(
        name="renamed repository folder: a data disc is copied to the "
             "pack cache",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"\nq\n", set_id=_SET_ID,
                      pack_cache=True,
                      discs=(DiscCat(_T1, (Vol(_T1, holds=False), Vol(_T2)),
                                     mark=1, data=True),),
                      catalog_from=_T1),
        expect=Outcome(exit=ReaderExit.READER_QUIT, restored=ANY,
                       discs=(_T2,),
                       says=(rf"copying disc {_T1} to the cache",),
                       never_says=(_FOREIGN, r"belongs to another archive")),
        replaces="new (#993)",
    ),
    # #967: the only catalog in reach cannot be read.  It is named as
    # not used, and the prompt has no catalog (tier 3 said "no catalog
    # available", and not why).
    Case(
        name="catalog choice: the only catalog is cut short",
        before=_MISSING_PACK_BEFORE,
        tree=_MISSING_PACK_TREE,
        faults=Faults(missing=(b"b",), answers=b"q\n",
                      discs=(DiscCat(_T2, (Vol(_T1), Vol(_T2)), mark=2,
                                     cut_to=5000),),
                      catalog_from=_T2),
        expect=Outcome(
            exit=ReaderExit.READER_QUIT, restored=ANY, discs=NO_CATALOG,
            says=(rf"WARNING: the catalog \S*{_T2}/catalog\.db is damaged "
                  r"or cut short, or this program cannot read it; not "
                  r"using it\.",)),
        replaces="new (#967)",
    ),
    Case(
        name="usage error",
        tree={b"f": File(b"f\n")},
        faults=Faults(usage_error=True),
        # No summary: tier 1 ends with its table of exit codes, tier 3
        # with argparse's "the following arguments are required".
        expect=Outcome(exit=ReaderExit.USAGE, restored={}),
        replaces="new",
    ),
    Case(
        name="second run over a finished restore",
        tree={b"a": File(b"alpha\n"), b"d/b": File(b"beta\n" * 50)},
        faults=Faults(first_run="latest", missing=(b"*",)),
        expect=Outcome(
            exit=ReaderExit.OK,
            summary=r"(?s)(skipping already-restored|already restored by "
                    r"an earlier run).*(" + _DONE + ")",
        ),
        replaces="new (#729, #787: no data pack is there for the second run)",
    ),
]


# ── Building the repository ─────────────────────────────────────────


class Skip(Exception):  # noqa: N818 - a reason, raised as pytest.skip by the test
    """A case or reader cannot run here; the message says why."""


@dataclass
class Built:
    """A case's repository, ready for each reader to run on a copy."""

    repo: Path
    src: Path | None          # the folder backed up (rustic), or None
    outside: Path             # the folder nothing may be written to
    snapshot_before: str | None
    missing_packs: list[str]  # pack ids the judged run must not find
    expected_tree: dict[bytes, Entry] | None   # what was backed up
    #: (pack id, byte offset, "truncated" | "corrupted" | "unreadable"):
    #: the pack is cut at the offset, has the byte there flipped, or is
    #: made mode 000, for the judged run.
    damaged_packs: list[tuple[str, int, str]] = field(default_factory=list)


def _outside(work: Path) -> Path:
    return work / "outside"


def _reset_outside(outside: Path) -> None:
    """The folder a case's links point to; nothing may change in it."""
    if outside.exists():
        for p in sorted(outside.rglob("*"), reverse=True):
            if p.is_dir() and not p.is_symlink():
                p.chmod(0o755)
        shutil.rmtree(outside)
    outside.mkdir()
    (outside / "file").write_bytes(b"OUTSIDE\n")
    (outside / "dir").mkdir()
    (outside / "dir").chmod(0o751)


def _materialise(tree: Tree, root: Path, outside: Path) -> None:
    """Make *tree* under *root* (which is emptied first)."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    base = os.fsencode(root)
    dir_modes: list[tuple[bytes, int]] = []
    for rel in sorted(tree):
        entry = tree[rel]
        full = os.path.join(base, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        if entry.kind == "file":
            with open(full, "wb") as f:
                f.write(entry.data or b"")
            if entry.mode is not None:
                os.chmod(full, entry.mode)
        elif entry.kind == "dir":
            os.makedirs(full, exist_ok=True)
            if entry.mode is not None:
                dir_modes.append((full, entry.mode))
        elif entry.kind == "link":
            target = (entry.data or b"").replace(b"@OUTSIDE@",
                                                 os.fsencode(outside))
            os.symlink(target, full)
        elif entry.kind == "fifo":
            os.mkfifo(full, 0o644 if entry.mode is None else entry.mode)
            if entry.mode is not None:
                os.chmod(full, entry.mode)
        elif entry.kind == "socket":
            # A socket path is capped at 108 bytes; bind it by its name
            # from inside its folder.
            here = os.getcwd()
            sock = socket.socket(socket.AF_UNIX)
            try:
                os.chdir(os.path.dirname(full))
                sock.bind(os.path.basename(full))
            finally:
                sock.close()
                os.chdir(here)
        else:
            raise ValueError(f"cannot make a {entry.kind} in a source tree")
    for full, mode in dir_modes:
        os.chmod(full, mode)


def _index(repo: Path, pwfile: Path,
           ) -> dict[str, list[tuple[str, str, int, int]]]:
    """pack id -> [(blob id, blob type, offset, length)], from rustic's
    own index."""
    def rustic(*args: str) -> str:
        return subprocess.run(
            ["rustic", "-r", str(repo), "--no-cache", *args,
             "--password-file", str(pwfile)],
            capture_output=True, text=True, check=True, timeout=60,
        ).stdout

    packs: dict[str, list[tuple[str, str, int, int]]] = {}
    for line in rustic("list", "index").split():
        m = re.search(r"[0-9a-f]{64}", line)
        if not m:
            continue
        doc = json.loads(rustic("cat", "index", m.group(0)))
        for pack in doc["packs"]:
            packs.setdefault(pack["id"], []).extend(
                (b["id"], b["type"], b["offset"], b["length"])
                for b in pack["blobs"])
    return packs


def build(case: Case, work: Path) -> Built:
    """Build the case's repository under *work* (once per case)."""
    work.mkdir(parents=True, exist_ok=True)
    outside = _outside(work)
    _reset_outside(outside)
    repo = work / "repo"
    if case.repo == "builder":
        from tests.unit import tier3_repo_builder

        layout: dict[str, list[str]] = {}
        repo = tier3_repo_builder.build_repo(
            work, dict(case.tree), pack_per_blob=case.pack_per_blob,
            layout=layout)
        gone: list[str] = []
        for spec in case.faults.missing:
            path, _, nth = os.fsdecode(spec).partition("#")
            packs = layout[path]
            gone += [packs[int(nth)]] if nth else packs
        assert not (case.faults.truncated or case.faults.corrupted
                    or case.faults.unreadable), \
            "pack damage is for rustic repositories"
        return Built(repo, None, outside, None, gone, None)

    if not shutil.which("rustic"):
        raise Skip("rustic not on PATH")
    src = work / "src"
    pwfile = work / "pw"
    pwfile.write_bytes(PASSWORD + b"\n")
    snapshot_before = None
    if case.before is not None:
        _materialise(case.before, src, outside)
        build_rustic_repo(src, repo, pwfile)
        snapshot_before = latest_snapshot_id(repo, pwfile)
        _materialise(case.tree, src, outside)    # type: ignore[arg-type]
        rustic_backup(src, repo, pwfile)
    else:
        _materialise(case.tree, src, outside)    # type: ignore[arg-type]
        build_rustic_repo(src, repo, pwfile)

    def blob_id(path: bytes) -> str:
        return hashlib.sha256(case.tree[path].data or b"").hexdigest()  # type: ignore[index]

    faults = case.faults
    index = (_index(repo, pwfile)
             if faults.missing or faults.truncated or faults.corrupted
             or faults.unreadable else {})
    missing: list[str] = []
    if faults.missing:
        if faults.missing == (b"*",):
            missing = [p for p, blobs in index.items()
                       if all(t == "data" for _, t, _, _ in blobs)]
        else:
            want = {blob_id(p) for p in faults.missing}
            missing = [p for p, blobs in index.items()
                       if any(b in want for b, _, _, _ in blobs)]
            # The other files must still be there to restore.
            assert all(all(t == "data" and b in want
                           for b, t, _, _ in index[p])
                       for p in missing), "a missing pack holds other blobs"
        assert missing, f"{case.name}: no pack to take away"
    damaged: list[tuple[str, int, str]] = []
    for how, paths in (("truncated", faults.truncated),
                       ("corrupted", faults.corrupted),
                       ("unreadable", faults.unreadable)):
        for path in paths:
            want_id = blob_id(path)
            hits = [(p, off, length) for p, blobs in index.items()
                    for b, _, off, length in blobs if b == want_id]
            assert len(hits) == 1, f"{case.name}: {path!r} is not one blob"
            pack, off, length = hits[0]
            assert len(index[pack]) == 1, f"{case.name}: {path!r} shares its pack"
            damaged.append((pack, off + length // 2, how))
    return Built(repo, src, outside, snapshot_before, missing,
                 walk(src, follow_outside=outside), damaged)


# ── Running a reader ────────────────────────────────────────────────


def tier1_binary() -> Path:
    """lcsas-restore as the differential tests find it: LCSAS_RESTORE_BIN,
    then recovery/build, then the committed recovery/bin binaries."""
    env_bin = os.environ.get("LCSAS_RESTORE_BIN")
    found = Path(env_bin) if env_bin else find_restore_bin()
    if found is None:
        raise Skip("no lcsas-restore binary; run `make -C recovery`")
    return found


_TIER3_SCRIPT: dict[str, Path] = {}


def tier3_script(work: Path) -> Path:
    """The generated python_reader.py, written once per session."""
    if "path" not in _TIER3_SCRIPT:
        from lcsas.restore.standalone_builder import build_standalone

        work.mkdir(parents=True, exist_ok=True)
        script = work / "python_reader.py"
        script.write_text(build_standalone())
        _TIER3_SCRIPT["path"] = script
    return _TIER3_SCRIPT["path"]


def _env() -> dict[str, str]:
    """The test's environment less every LCSAS_* setting."""
    return {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}


def _can_sudo_mount() -> bool:
    if shutil.which("mount") is None or shutil.which("umount") is None:
        return False
    if os.geteuid() == 0:
        return True
    res = subprocess.run(["sudo", "-n", "true"], capture_output=True,
                         timeout=10)
    return res.returncode == 0


@contextlib.contextmanager
def _small_tmpfs(mnt: Path):  # type: ignore[no-untyped-def]
    """A 1 MiB tmpfs at *mnt*, owned by this user; lazily unmounted."""
    sudo = [] if os.geteuid() == 0 else ["sudo", "-n"]
    mnt.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [*sudo, "mount", "-t", "tmpfs", "-o",
         f"size=1m,uid={os.getuid()},gid={os.getgid()},mode=0700",
         "tmpfs", str(mnt)],
        check=True, capture_output=True, timeout=30,
    )
    try:
        yield mnt
    finally:
        subprocess.run([*sudo, "umount", "-l", str(mnt)],
                       capture_output=True, timeout=30)
        with contextlib.suppress(OSError):
            mnt.rmdir()


def _command(reader: str, repo: Path, target: Path, pwfile: Path,
             snapshot: str | None, faults: Faults, empty: Path,
             work: Path) -> list[str]:
    mounts = work / "mnt" if faults.discs else empty
    if reader == "tier1":
        cmd = [str(tier1_binary()), "--repo", str(repo),
               "--password-file", str(pwfile),
               "--mount-parent", str(mounts)]
        if faults.answers is not None:
            cmd += ["--interactive", "on", "--auto-mount", "off"]
        else:
            cmd += ["--interactive", "off"]
    else:
        cmd = [sys.executable, "-I", str(tier3_script(work.parent)),
               "--repo", str(repo), "--password-file", str(pwfile),
               "--interactive", "on" if faults.answers is not None else "off"]
        if faults.discs:
            cmd += ["--mount-point", str(repo / "data"),
                    "--mount-point", str(mounts)]
    if faults.catalog is not None:
        cmd += ["--catalog", str(work / "catalog.db")]
    if faults.catalog_from is not None:
        cmd += ["--catalog", str(mounts / faults.catalog_from / "catalog.db")]
    if snapshot:
        cmd += ["--snapshot", snapshot]
    if not faults.usage_error:
        cmd += ["--restore-folder", str(target)]
    return cmd


def _write_disc_catalogs(discs: tuple[DiscCat, ...], packs: list[str],
                         mounts: Path, repo_id: str, repo: Path) -> None:
    """Each disc of *discs* as a folder in *mounts* holding its catalog,
    whose file times are ``age`` seconds apart (#950).  A data disc also
    holds a copy of the first pack of *repo* not in *packs* (#993)."""
    newest = 1_700_000_000
    for disc in discs:
        folder = mounts / disc.name
        folder.mkdir(parents=True)
        if disc.data:
            pack = min(p for p in (repo / "data").rglob("*")
                       if p.is_file() and p.name not in packs)
            (folder / "data" / pack.parent.name).mkdir(parents=True)
            shutil.copyfile(pack, folder / "data" / pack.parent.name / pack.name)
            (folder / "volume_info.json").write_text(
                json.dumps({"label": disc.name}))
        path = folder / "catalog.db"
        write_catalog(path, packs, disc.vols, knows_pack=disc.knows_pack,
                      repo_id="another-archive" if disc.foreign else repo_id,
                      mark=disc.mark)
        if disc.cut_to is not None:
            assert path.stat().st_size > disc.cut_to, "the cut is a cut"
            os.truncate(path, disc.cut_to)
        os.utime(path, (newest - disc.age, newest - disc.age))


def _run(cmd: list[str], stdin: bytes,
         env: Mapping[str, str] | None = None,
         ) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(cmd, input=stdin, capture_output=True,
                          env={**_env(), **(env or {})}, timeout=300)


def run_reader(reader: str, case: Case, built: Built, work: Path) -> Outcome:
    """Run *reader* on a copy of *built* in *work*; return what it did."""
    if case.needs == "root" and os.geteuid() != 0:
        raise Skip("needs root: run by hand with sudo")
    if case.needs == "user" and os.geteuid() == 0:
        raise Skip("as root this case is a different one")
    if reader == "tier1":
        tier1_binary()
    if case.faults.target_full and not _can_sudo_mount():
        raise Skip("needs passwordless sudo, mount and umount (a tmpfs)")
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    repo = work / "repo"
    shutil.copytree(built.repo, repo, symlinks=True)
    pwfile = work / "pw"
    pwfile.write_bytes(case.faults.password_file)
    good_pw = work / "pw-good"
    if case.repo == "builder":
        from tests.unit.tier3_repo_builder import PASSWORD as BUILDER_PW

        good_pw.write_bytes(BUILDER_PW + b"\n")
        if case.faults.password_file == PASSWORD + b"\n":
            pwfile.write_bytes(BUILDER_PW + b"\n")
    else:
        good_pw.write_bytes(PASSWORD + b"\n")
    empty = work / "no-discs"
    empty.mkdir()
    # #993: the set id the catalogs record: the --repo folder's name,
    # unless the case names the folder otherwise.
    set_id = case.faults.set_id or repo.name
    if case.faults.catalog is not None:
        write_catalog(work / "catalog.db", built.missing_packs,
                      case.faults.catalog,
                      knows_pack=case.faults.catalog_knows_pack,
                      repo_id=set_id)
    _write_disc_catalogs(case.faults.discs, built.missing_packs,
                         work / "mnt", set_id, repo)
    _reset_outside(built.outside)
    outside_before = walk(built.outside)

    with contextlib.ExitStack() as stack:
        if case.faults.target_full:
            target = stack.enter_context(_small_tmpfs(work / "full"))
        else:
            target = work / "target"
            target.mkdir()
        if case.faults.first_run:
            snap = (built.snapshot_before if case.faults.first_run == "before"
                    else None)
            first = _run(_command(reader, repo, target, good_pw, snap,
                                  Faults(), empty, work), b"")
            if first.returncode != 0:
                raise AssertionError(
                    f"case {case.name!r}, reader {reader}: the first run "
                    f"exited {first.returncode}:\n"
                    + first.stderr.decode("utf-8", "replace"))
        for pack in built.missing_packs:
            (repo / "data" / pack[:2] / pack).unlink()
        for pack, at, how in built.damaged_packs:
            if how == "unreadable":
                os.chmod(repo / "data" / pack[:2] / pack, 0)
                continue
            with open(repo / "data" / pack[:2] / pack, "r+b") as f:
                if how == "truncated":
                    f.truncate(at)
                else:
                    f.seek(at)
                    byte = f.read(1)
                    f.seek(at)
                    f.write(bytes([byte[0] ^ 0xFF]))
        for rel in case.faults.damage:
            hit = repo / rel
            if hit.is_dir():
                for child in hit.iterdir():
                    child.unlink()
            else:
                hit.unlink()
        cmd = _command(reader, repo, target, pwfile, None, case.faults,
                       empty, work)
        if case.faults.snapshot is not None:
            # Here, not in _command: an empty id is an argument too.
            cmd += ["--snapshot", case.faults.snapshot]
        env = ({"LCSAS_PACK_CACHE_DIR": str(work / "cache")}
               if case.faults.pack_cache else None)
        res = _run(cmd, case.faults.answers or b"", env)
        output = (res.stdout + res.stderr).decode("utf-8", "replace")
        root = target
        if built.src is not None:
            root = target / built.src.relative_to(built.src.anchor)
        restored = walk(root) if root.is_dir() else {}
        if root == target:
            # The list of items not restored sits in the target itself.
            restored.pop(FAILURES_NAME.encode(), None)
        failures = _read_failures(target, root)
    wrote_outside = walk(built.outside) != outside_before
    return Outcome(
        exit=res.returncode,
        restored=restored,
        failures=failures,
        summary="\n".join(output.rstrip("\n").splitlines()[-8:]),
        wrote_outside=wrote_outside,
        discs=named_discs(output),
        output=output,
    )


def run_tier1(case: Case, built: Built, work: Path) -> Outcome:
    """Run ``lcsas-restore`` (tier 1) on the case."""
    return run_reader("tier1", case, built, work)


def run_tier3(case: Case, built: Built, work: Path) -> Outcome:
    """Run the generated ``python_reader.py`` with ``python3 -I``."""
    return run_reader("tier3", case, built, work)


# ── Reading what a reader did ───────────────────────────────────────


def _entry(full: bytes) -> Entry:
    """What is at *full*, by lstat; a FIFO is never opened."""
    st = os.lstat(full)
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return Entry("link", os.readlink(full), None)
    if stat.S_ISDIR(st.st_mode):
        return Entry("dir", None, mode)
    if stat.S_ISREG(st.st_mode):
        with open(full, "rb") as f:
            return Entry("file", _content(f.read()), mode)
    if stat.S_ISFIFO(st.st_mode):
        return Entry("fifo", None, mode)
    if stat.S_ISSOCK(st.st_mode):
        return Entry("socket", None, None)
    if stat.S_ISCHR(st.st_mode):
        return Entry("chardev", str(st.st_rdev).encode(), mode)
    if stat.S_ISBLK(st.st_mode):
        return Entry("blockdev", str(st.st_rdev).encode(), mode)
    return Entry("other", None, mode)


def walk(root: Path, follow_outside: Path | None = None) -> dict[bytes, Entry]:
    """Map each path under *root* (relative, bytes) to its :class:`Entry`.

    With *follow_outside*, a link target that starts with it is written
    back as ``@OUTSIDE@`` (so a source tree compares with a restore).
    """
    out: dict[bytes, Entry] = {}
    base = os.fsencode(root)
    for dirpath, dirnames, filenames in os.walk(base):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            out[os.path.relpath(full, base)] = _entry(full)
    if follow_outside is not None:
        outside = os.fsencode(follow_outside)
        for rel, e in out.items():
            if e.kind == "link" and e.data and e.data.startswith(outside):
                out[rel] = Entry("link", b"@OUTSIDE@" + e.data[len(outside):])
    return out


def _read_failures(target: Path, root: Path) -> tuple[tuple[str, str], ...]:
    """The rows of RESTORE_FAILURES.txt as (path under *root*, reason).

    Both readers write the path as the full path of the item; it is
    shown relative to the restored root.  The header (its second line
    names the reader, by design) is skipped.
    """
    listing = target / FAILURES_NAME
    if not listing.is_file():
        return ()
    lines = listing.read_text(encoding="utf-8", errors="replace").splitlines()
    try:
        start = lines.index("path\treason\tblob_id") + 1
    except ValueError:
        return (("<no header>", "\n".join(lines[:3])),)
    rows = []
    prefixes = [str(root) + "/", str(target) + "/"]
    for line in lines[start:]:
        if not line:
            continue
        path, _, rest = line.partition("\t")
        reason = rest.rsplit("\t", 1)[0] if "\t" in rest else rest
        for prefix in prefixes:
            if path.startswith(prefix):
                path = path[len(prefix):]
                break
        rows.append((path, reason))
    return tuple(sorted(rows))


# ── Comparing ───────────────────────────────────────────────────────


def _expected_tree(want: Outcome, built: Built) -> Mapping[bytes, Entry] | str:
    if want.restored is not None:
        return want.restored
    assert built.expected_tree is not None, "a builder case states its tree"
    gone = [os.fsencode(p) for p, _ in want.failures]
    return {rel: e for rel, e in built.expected_tree.items()
            if not any(rel == g or rel.startswith(g + b"/") for g in gone)}


def _tree_diff(want: Mapping[bytes, Entry], got: Mapping[bytes, Entry]) -> str:
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    wrong = sorted(k for k in want if k in got and not want[k].matches(got[k]))
    if not (missing or extra or wrong):
        return ""
    lines = []
    if missing:
        lines.append(f"    missing: {missing}")
    if extra:
        lines.append(f"    extra:   {extra}")
    for k in wrong:
        lines.append(f"    {k!r}: want {want[k]!r}, got {got[k]!r}")
    return "\n".join(lines)


def mismatches(case: Case, reader: str, got: Outcome, built: Built) -> list[str]:
    """How *got* differs from what *case* expects of *reader*."""
    want = case.expected(reader)
    out: list[str] = []
    if got.exit != want.exit:
        out.append(f"exit {got.exit}, want {int(want.exit)}")
    tree = _expected_tree(want, built)
    if tree != ANY:
        assert isinstance(got.restored, Mapping)
        diff = _tree_diff(tree, got.restored)       # type: ignore[arg-type]
        if diff:
            out.append("restored tree differs:\n" + diff)
    want_rows = sorted(want.failures)
    got_rows = list(got.failures)
    if len(want_rows) != len(got_rows) or any(
        gp != wp or not gr.startswith(wr)
        for (wp, wr), (gp, gr) in zip(want_rows, got_rows, strict=True)
    ):
        out.append(f"{FAILURES_NAME} rows {got_rows}, want (reason prefixes) "
                   f"{want_rows}")
    if want.summary and not re.search(want.summary, got.summary):
        out.append(f"last lines do not match /{want.summary}/:\n{got.summary}")
    if got.wrote_outside != want.wrote_outside:
        out.append(f"wrote outside the target: {got.wrote_outside}")
    if want.discs is not None and got.discs != want.discs:
        out.append(f"the disc prompt names {got.discs!r}, want {want.discs!r}")
    for rx in want.says:
        if not re.search(rx, got.output):
            out.append(f"does not say /{rx}/")
    for rx in want.never_says:
        if re.search(rx, got.output):
            out.append(f"says /{rx}/")
    return out


def disagreements(case: Case, outcomes: Mapping[str, Outcome]) -> list[str]:
    """How the readers' outcomes differ: exit code, restored tree (unless
    the case leaves it open), the rows of RESTORE_FAILURES.txt (whole
    reasons), writing outside the target and, when the case has a disc
    catalog, the discs the prompt names.  The other words printed differ
    by design and are not compared."""
    (r1, o1), (r2, o2) = outcomes.items()
    out: list[str] = []
    if o1.exit != o2.exit:
        out.append(f"exit: {r1} {o1.exit}, {r2} {o2.exit}")
    if case.expect.restored != ANY:
        diff = _tree_diff(o1.restored, o2.restored)  # type: ignore[arg-type]
        if diff:
            out.append(f"restored tree ({r1} as wanted, {r2} as got):\n{diff}")
    if o1.failures != o2.failures:
        out.append(f"{FAILURES_NAME}: {r1} {list(o1.failures)}, "
                   f"{r2} {list(o2.failures)}")
    if o1.wrote_outside != o2.wrote_outside:
        out.append(f"wrote outside: {r1} {o1.wrote_outside}, "
                   f"{r2} {o2.wrote_outside}")
    if ((case.faults.catalog is not None or case.faults.discs)
            and o1.discs != o2.discs):
        out.append(f"discs named: {r1} {o1.discs!r}, {r2} {o2.discs!r}")
    return out


__all__ = [
    "ANY", "CASES", "READERS", "Built", "Case", "Entry", "Faults", "Outcome",
    "ReaderExit", "Skip", "build", "disagreements", "mismatches",
    "run_reader", "run_tier1", "run_tier3", "walk",
]
