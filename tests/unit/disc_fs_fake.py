"""An in-memory filesystem for the Python reader's disc search (#885).

Not a test module itself (no ``test_`` prefix), and not bundled into the
reader: test code only.

``FakeDiscFs`` is ``_DiscFs`` with its primitives (the ``_``-prefixed
methods that make the OS calls) replaced by a dict of paths.  The public
methods, and so the guards that turn an ``OSError`` into "not there",
are the real ones: a fault set here is raised as a real ``OSError``
underneath them, exactly where the OS would raise it.

Faults are set per path and per public method (``fail``), optionally
for everything below the path too (a folder that cannot be entered).
Every fault that fires is recorded in ``hits``, so a test can tell a
fault that was met from one the search never came near.

Paths are compared by their text (``posixpath.normpath``), as the search
compares them; there are no links.
"""

from __future__ import annotations

import errno
import hashlib
import os
import posixpath
import sqlite3
import stat
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from lcsas.restore._disc_search import _DiscFs

#: The public methods of _DiscFs: the interface of the seam.  A fault
#: names one of them, or ``ANY`` for all.
METHODS = (
    "is_file", "is_dir", "listdir", "stat", "readable", "realpath",
    "abspath", "read_range", "open_catalog", "copy_checked", "free_bytes",
    "tree_bytes", "make_dirs", "scratch_dir", "write_file", "remove_tree",
)
ANY = "*"

#: Where scratch_dir makes its folders.
SCRATCH = "/fake-tmp"


@dataclass(frozen=True)
class Fault:
    """*method* on *path* (and, with *below*, on anything under it)
    raises ``OSError(err)``."""

    method: str
    path: str
    err: int
    below: bool


class FakeDiscFs(_DiscFs):
    """A tree of folders and files in memory, with per-path faults.

    *sqlite_dir* is a real folder: SQLite cannot open bytes in memory
    on every Python this suite runs on, so ``open_catalog`` writes the
    catalog's bytes there and opens that copy.
    """

    def __init__(self, sqlite_dir: Path) -> None:
        self.files: dict[str, bytes] = {}
        self.dirs: set[str] = {"/"}
        self.faults: list[Fault] = []
        self.hits: list[tuple[str, str]] = []
        self._sqlite_dir = sqlite_dir
        self._serial = 0

    # ── Building the tree ────────────────────────────────────────

    @staticmethod
    def key(path: Path | str) -> str:
        return posixpath.normpath(os.fspath(path))

    def add_dir(self, path: Path | str) -> None:
        k = self.key(path)
        while k not in self.dirs:
            assert k not in self.files, k
            self.dirs.add(k)
            k = posixpath.dirname(k)

    def add_file(self, path: Path | str, data: bytes) -> None:
        k = self.key(path)
        self.add_dir(posixpath.dirname(k))
        assert k not in self.dirs, k
        self.files[k] = data

    def mirror(self, real: Path) -> None:
        """Put the real folder *real*, and everything in it, into the
        tree at the same paths."""
        self.add_dir(real)
        for dirpath, dirnames, filenames in os.walk(real):
            for d in dirnames:
                self.add_dir(Path(dirpath) / d)
            for f in filenames:
                self.add_file(Path(dirpath) / f, (Path(dirpath) / f).read_bytes())

    def move(self, src: Path | str, dst: Path | str) -> None:
        """Rename folder *src* to *dst*: a disc put in the drive."""
        s, d = self.key(src), self.key(dst)
        self.add_dir(posixpath.dirname(d))
        for k in [k for k in self.dirs if self._under(k, s)]:
            self.dirs.remove(k)
            self.dirs.add(d + k[len(s):])
        for k in [k for k in self.files if self._under(k, s)]:
            self.files[d + k[len(s):]] = self.files.pop(k)

    def fail(
        self, method: str, path: Path | str, err: int, *, below: bool = False,
    ) -> None:
        """From now on *method* (or ``ANY``) on *path* raises *err*."""
        assert method == ANY or method in METHODS, method
        self.faults.append(Fault(method, self.key(path), err, below))

    def shut(self, path: Path | str, err: int = errno.EACCES) -> None:
        """Folder *path* cannot be entered (mode 000 for this user): it
        can be seen, but not listed, and nothing below it can be
        touched."""
        k = self.key(path)
        self.fail("listdir", k, err)
        # A fault path ending in "/" matches only what is below it.
        self.faults.append(Fault(ANY, k.rstrip("/") + "/", err, True))

    # ── Faults ───────────────────────────────────────────────────

    @staticmethod
    def _under(k: str, top: str) -> bool:
        return k == top or k.startswith(top.rstrip("/") + "/")

    def _check(self, method: str, *paths: Path | str) -> None:
        for path in paths:
            k = self.key(path)
            for f in self.faults:
                if f.method not in (method, ANY):
                    continue
                if k == f.path or (f.below and self._under(k, f.path)):
                    self.hits.append((method, k))
                    raise OSError(f.err, os.strerror(f.err), k)

    def _missing(self, k: str) -> OSError:
        if k in self.dirs:
            return OSError(errno.EISDIR, os.strerror(errno.EISDIR), k)
        return OSError(errno.ENOENT, os.strerror(errno.ENOENT), k)

    def _children(self, k: str) -> list[str]:
        names = {
            p[len(k.rstrip("/")) + 1:].split("/", 1)[0]
            for p in (*self.dirs, *self.files)
            if p != k and self._under(p, k)
        }
        return list(names)

    # ── The primitives ───────────────────────────────────────────

    def _is_file(self, path: Path) -> bool:
        self._check("is_file", path)
        return self.key(path) in self.files

    def _is_dir(self, path: Path) -> bool:
        self._check("is_dir", path)
        return self.key(path) in self.dirs

    def _listdir(self, path: Path) -> list[str]:
        self._check("listdir", path)
        k = self.key(path)
        if k not in self.dirs:
            if k in self.files:
                raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), k)
            raise self._missing(k)
        return self._children(k)

    def _stat(self, path: Path) -> os.stat_result:
        self._check("stat", path)
        k = self.key(path)
        ino = int(hashlib.sha256(k.encode()).hexdigest()[:12], 16)
        if k in self.files:
            mode, size = stat.S_IFREG | 0o644, len(self.files[k])
        elif k in self.dirs:
            mode, size = stat.S_IFDIR | 0o755, 0
        else:
            raise self._missing(k)
        return os.stat_result((mode, ino, 1, 1, 0, 0, size, 0, 0, 0))

    def _readable(self, path: Path) -> bool:
        self._check("readable", path)
        k = self.key(path)
        return k in self.files or k in self.dirs

    def _realpath(self, path: Path) -> str:
        self._check("realpath", path)
        return self.key(path)

    def _abspath(self, path: Path) -> str:
        self._check("abspath", path)
        return self.key(path)

    def _read_range(self, path: Path, offset: int, length: int) -> bytes:
        self._check("read_range", path)
        k = self.key(path)
        if k not in self.files:
            raise self._missing(k)
        data = self.files[k]
        return data[offset:] if length < 0 else data[offset:offset + length]

    def _connect(self, path: Path) -> sqlite3.Connection:
        self._check("open_catalog", path)
        k = self.key(path)
        if k not in self.files:
            raise sqlite3.OperationalError("unable to open database file")
        self._serial += 1
        real = self._sqlite_dir / f"catalog-{self._serial}.db"
        real.write_bytes(self.files[k])
        return sqlite3.connect(f"file:{real}?mode=ro&immutable=1", uri=True)

    def _copy(
        self, src: Path, dst: Path, pack_id: str, tick: Callable[[int], None],
    ) -> bool:
        self._check("copy_checked", src, dst)
        k = self.key(src)
        if k not in self.files:
            raise self._missing(k)
        data = self.files[k]
        tick(len(data))
        if hashlib.sha256(data).hexdigest() != pack_id:
            return False
        self.add_file(dst, data)
        return True

    def _disk_usage(self, path: Path) -> tuple[int, int]:
        self._check("free_bytes", path)
        return 1 << 40, 1 << 41

    def _tree_bytes(self, path: Path) -> int:
        self._check("tree_bytes", path)
        k = self.key(path)
        return sum(len(v) for p, v in self.files.items() if self._under(p, k))

    def _makedirs(self, path: Path) -> None:
        self._check("make_dirs", path)
        k = self.key(path)
        up = k
        while up != "/":
            if up in self.files:
                raise OSError(errno.ENOTDIR, os.strerror(errno.ENOTDIR), k)
            up = posixpath.dirname(up)
        self.add_dir(k)

    def _mkdtemp(self, prefix: str) -> Path:
        self._serial += 1
        made = f"{SCRATCH}/{prefix}{self._serial}"
        self._check("scratch_dir", made)
        self.add_dir(made)
        return Path(made)

    def _write(self, path: Path, data: bytes) -> None:
        self._check("write_file", path)
        k = self.key(path)
        if posixpath.dirname(k) not in self.dirs:
            raise self._missing(posixpath.dirname(k))
        self.files[k] = data

    def _rmtree(self, path: Path) -> None:
        self._check("remove_tree", path)
        k = self.key(path)
        self.dirs = {p for p in self.dirs if not self._under(p, k)}
        self.files = {p: v for p, v in self.files.items() if not self._under(p, k)}
