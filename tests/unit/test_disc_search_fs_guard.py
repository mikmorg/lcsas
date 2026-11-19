"""Every filesystem touch of the disc search goes through ``_DiscFs`` (#885).

The rc10 sweep failed four times (#877, #880, #881, #882) on a probe of
the Python reader's disc search that raised where it should have said
"no": each touch carried its own guard and one was missed each time.
Since #885 the guards live in one class, ``_DiscFs``, whose methods
never raise ``OSError``.  This check keeps it so: anywhere in
``_disc_search.py`` outside ``_DiscFs``, it fails on

* a call into ``os`` (``os.stat``, ``os.listdir``, ``os.path.*``, ...),
  ``shutil``, ``tempfile`` or ``atexit``, or ``sqlite3.connect``;
* the builtin ``open(``;
* a filesystem method of a path (``.is_file()``, ``.is_dir()``,
  ``.exists()``, ``.stat()``, ``.mkdir()``, ``.resolve()``, ...),
  unless it is called on ``self._fs``;
* a call of a ``_DiscFs`` primitive (``self._fs._stat(...)``), which
  goes around its guard;
* an ``except`` (or ``contextlib.suppress``) for ``OSError`` or one of
  its subclasses: the guards belong in ``_DiscFs``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

SRC = (
    Path(__file__).resolve().parents[2]
    / "src" / "lcsas" / "restore" / "_disc_search.py"
)

#: Modules whose every call touches the filesystem or the process.
_MODULES = {"os", "shutil", "tempfile", "atexit"}
#: Methods of pathlib.Path (and file objects) that touch the filesystem.
_PATH_METHODS = {
    "is_file", "is_dir", "exists", "stat", "lstat", "read_bytes",
    "read_text", "write_bytes", "write_text", "open", "mkdir", "rmdir",
    "iterdir", "glob", "rglob", "resolve", "unlink", "rename", "touch",
    "samefile", "chmod", "readlink", "is_symlink", "owner", "absolute",
}
_OS_ERRORS = {
    "OSError", "IOError", "EnvironmentError", "PermissionError",
    "FileNotFoundError", "IsADirectoryError", "NotADirectoryError",
    "FileExistsError", "InterruptedError", "BlockingIOError",
}


def _root_name(node: ast.AST) -> str | None:
    """``os`` for ``os.path.join``: the name an attribute chain starts at."""
    while isinstance(node, ast.Attribute):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _on_self_fs(node: ast.Attribute) -> bool:
    v = node.value
    return (
        isinstance(v, ast.Attribute) and v.attr == "_fs"
        and isinstance(v.value, ast.Name) and v.value.id == "self"
    )


def _names(node: ast.AST | None) -> set[str]:
    if node is None:
        return set()
    return {
        n.id if isinstance(n, ast.Name) else n.attr
        for n in ast.walk(node) if isinstance(n, ast.Name | ast.Attribute)
    }


def _touches(node: ast.AST) -> Iterator[tuple[int, str]]:
    """(line, what) for each filesystem touch or OSError guard in *node*."""
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name) and f.id == "open":
                yield n.lineno, "open("
            elif isinstance(f, ast.Attribute):
                root = _root_name(f)
                if root in _MODULES:
                    yield n.lineno, f"{ast.unparse(f)}("
                elif root == "sqlite3" and f.attr == "connect":
                    yield n.lineno, "sqlite3.connect("
                elif root == "contextlib" and f.attr == "suppress" and (
                    _names(ast.Tuple(elts=n.args)) & _OS_ERRORS
                ):
                    yield n.lineno, "contextlib.suppress(OSError)"
                elif _on_self_fs(f) and f.attr.startswith("_"):
                    yield n.lineno, f"self._fs.{f.attr}("
                elif f.attr in _PATH_METHODS and not _on_self_fs(f):
                    yield n.lineno, f".{f.attr}("
        elif isinstance(n, ast.ExceptHandler):
            if n.type is None or _names(n.type) & (_OS_ERRORS | {"Exception"}):
                yield n.lineno, "except OSError"


def _split() -> tuple[ast.ClassDef, list[ast.stmt]]:
    tree = ast.parse(SRC.read_text(), filename=str(SRC))
    (fs,) = [
        n for n in tree.body
        if isinstance(n, ast.ClassDef) and n.name == "_DiscFs"
    ]
    return fs, [n for n in tree.body if n is not fs]


def test_no_filesystem_touch_outside_the_adapter() -> None:
    _fs, rest = _split()
    found = sorted(t for node in rest for t in _touches(node))
    assert not found, (
        "the disc search touches the filesystem outside _DiscFs "
        "(route it through a _DiscFs method, #885): "
        + ", ".join(f"line {ln}: {what}" for ln, what in found)
    )


def test_the_check_sees_the_adapters_own_touches() -> None:
    """The matcher is not blind: inside _DiscFs it finds the calls it
    forbids elsewhere."""
    fs, _rest = _split()
    seen = {what for _ln, what in _touches(fs)}
    for what in (
        "open(", "os.listdir(", "os.stat(", "os.path.realpath(",
        "os.path.abspath(", "os.access(", "shutil.disk_usage(",
        "tempfile.mkdtemp(", "sqlite3.connect(", ".is_file(", ".is_dir(",
        ".mkdir(", "except OSError", "contextlib.suppress(OSError)",
    ):
        assert what in seen, (what, sorted(seen))


def test_the_check_finds_a_planted_touch() -> None:
    planted = ast.parse(
        "class DiscSearch:\n"
        "    def f(self, p):\n"
        "        self._fs.is_file(p)\n"        # allowed
        "        if p.exists():\n"             # not
        "            os.path.isdir(p)\n"       # not
        "        try:\n"
        "            pass\n"
        "        except PermissionError:\n"    # not
        "            pass\n"
        "        self._fs._stat(p)\n"          # not: no guard
    )
    assert sorted(_touches(planted)) == [
        (4, ".exists("), (5, "os.path.isdir("), (8, "except OSError"),
        (10, "self._fs._stat("),
    ]
