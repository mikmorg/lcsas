#!/usr/bin/env python3
"""Check a drill restore against the kit's manifest.

usage: verify_restore.py <manifest.json> <restored dir> [--repo NAME]
                         [--no-metadata]

The kit writes manifest.json from the corpus as it was at the newest backup
(build_kit.py, #834).  For each backup set it lists every item by its path
inside the set, as bytes: files (sha256, size, mode), folders (mode; empty
ones too), symlinks (target) and hardlink groups.  It also lists what the
first backup had and the newest one deleted ("absent").

A restore may nest a set under extra folders (the restore target, a
snapshot path), so each set is found as the folder named after it that
holds the most of its top-level items.  Inside it every path is compared
exactly, as bytes; links are never followed.

Checks, per item: it is there (MISSING), it is the same kind of item (TYPE),
same content (DIFFERS), same mode (MODE), same link target (LINK), the
hardlinked names are one file (NOT LINKED), and nothing the newest backup
deleted came back (OLD: the restore used an older snapshot).  Items in the
folder that the manifest does not list are printed as EXTRA but do not fail
the check.

--no-metadata skips modes, symlinks and hardlinks, and says so: use it for
a folder copied off Windows, which keeps none of them.

Exits 0 only when every checked item (of --repo, if given) is right, 1 when
any is wrong.  Exits 2, with a message, when there is nothing to compare:
the folder does not exist or holds no files, the manifest (or --repo)
selects no items, or no set is in the folder.  "0/0 restored intact" is
never a pass (#833).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

EXIT_MISMATCH = 1
EXIT_NOTHING_COMPARED = 2
MANIFEST_FORMAT = "lcsas-drill-manifest/1"
_SEP = os.sep.encode()


def _sha256(path: bytes) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _show(path: str) -> str:
    """A path as text one can read: control characters and bytes that are
    not UTF-8 are shown as \\xNN."""
    out = []
    for ch in path:
        code = ord(ch)
        if 0xDC80 <= code <= 0xDCFF:          # a byte os.fsdecode kept
            out.append(f"\\x{code - 0xDC00:02x}")
        elif code < 32 or code == 127:
            out.append(f"\\x{code:02x}")
        else:
            out.append(ch)
    return "".join(out)


def _rel(path: bytes, start: bytes) -> str:
    """*path* relative to *start*, with "/" between parts (as the manifest)."""
    rel = os.fsdecode(os.path.relpath(path, start))
    return rel.replace(os.sep, "/") if os.sep != "/" else rel


def _error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    print("Nothing was checked.  This is not a pass.", file=sys.stderr)
    return EXIT_NOTHING_COMPARED


def _read_manifest(path: Path) -> dict[str, dict]:
    """{set name: {"entries": {path: item}, "absent": [path, ...]}}."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{path} is not a drill manifest: {exc}") from exc
    if not isinstance(data, dict) or data.get("format") != MANIFEST_FORMAT:
        raise ValueError(
            f"{path} is not a {MANIFEST_FORMAT} manifest (a kit built before "
            "#834 wrote manifest.sha256: check it with that kit's own "
            "verify_restore.py)"
        )
    sets = data.get("sets")
    if not isinstance(sets, dict):
        raise ValueError(f"{path} has no 'sets'")
    return sets


def _find_set_root(restored: bytes, name: str, top: set[str]) -> bytes | None:
    """The folder named *name* holding the most of the set's top-level
    items; the shallowest one on a tie."""
    want = os.fsencode(name)
    best: tuple[int, int, bytes] | None = None
    for parent, dirs, _files in os.walk(restored, followlinks=False):
        dirs.sort()
        if want not in dirs:
            continue
        candidate = os.path.join(parent, want)
        if os.path.islink(candidate):
            continue
        try:
            children = {os.fsdecode(c) for c in os.listdir(candidate)}
        except OSError:
            continue
        score = len(children & top)
        if score == 0:
            continue
        key = (score, -candidate.count(_SEP), candidate)
        if best is None or key[:2] > best[:2]:
            best = key
    return best[2] if best else None


class Checker:
    def __init__(self, metadata: bool) -> None:
        self.metadata = metadata
        self.checked = 0
        self.bad: set[tuple[str, str]] = set()
        self.skipped_links = 0

    def fail(self, tag: str, set_name: str, rel: str, why: str = "") -> None:
        self.bad.add((set_name, rel))
        print(f"{tag:<8} {set_name}/{_show(rel)}" + (f"  ({why})" if why else ""))

    def check_set(self, set_name: str, root: bytes | None,
                  entries: dict[str, dict], absent: list[str]) -> None:
        if root is None:
            print(f"MISSING  {set_name}/  (the whole backup set is not in "
                  f"the folder: {len(entries)} items)")
        groups: dict[int, list[str]] = {}
        for rel, item in entries.items():
            if item.get("type") == "symlink" and not self.metadata:
                self.skipped_links += 1
                continue
            self.checked += 1
            if root is None:
                self.bad.add((set_name, rel))
                continue
            path = os.path.join(root, os.fsencode(rel))
            try:
                st = os.lstat(path)
            except OSError:
                self.fail("MISSING", set_name, rel)
                continue
            if self._check_item(set_name, rel, item, path, st) and "hardlink" in item:
                groups.setdefault(item["hardlink"], []).append(rel)
        if self.metadata and root is not None:
            for group in groups.values():
                ids = set()
                for rel in group:
                    st = os.lstat(os.path.join(root, os.fsencode(rel)))
                    ids.add((st.st_dev, st.st_ino))
                if len(ids) != 1:
                    for rel in group:
                        self.fail("NOT LINKED", set_name, rel,
                                  "hardlinked names came back as separate files")
        for rel in absent:
            self.checked += 1
            if root is not None and os.path.lexists(
                    os.path.join(root, os.fsencode(rel))):
                self.fail("OLD", set_name, rel,
                          "the newest backup deleted it: the restore used "
                          "an older snapshot")

    def _check_item(self, set_name: str, rel: str, item: dict, path: bytes,
                    st: os.stat_result) -> bool:
        """Type, content, link target and mode of one item that is there.
        True when its type is right."""
        kind = item.get("type")
        is_kind = {
            "file": (stat.S_ISREG, "a file"),
            "dir": (stat.S_ISDIR, "a folder"),
            "symlink": (stat.S_ISLNK, "a symlink"),
            "fifo": (stat.S_ISFIFO, "a pipe (FIFO)"),
        }.get(str(kind))
        if is_kind is None:
            self.fail("TYPE", set_name, rel, f"unknown type {kind!r} in the manifest")
            return False
        if not is_kind[0](st.st_mode):
            self.fail("TYPE", set_name, rel, f"expected {is_kind[1]}")
            return False
        if kind == "file" and (st.st_size != item["size"]
                               or _sha256(path) != item["sha256"]):
            self.fail("DIFFERS", set_name, rel, "content")
        if kind == "symlink":
            got = os.readlink(path)
            if got != os.fsencode(item["target"]):
                self.fail("LINK", set_name, rel,
                          f"points to '{_show(os.fsdecode(got))}', "
                          f"expected '{_show(item['target'])}'")
        if self.metadata and "mode" in item:
            got_mode = stat.S_IMODE(st.st_mode)
            if got_mode != item["mode"]:
                self.fail("MODE", set_name, rel,
                          f"{got_mode:04o}, expected {item['mode']:04o}")
        return True


def _extras(restored: bytes, roots: dict[str, bytes], chosen: set[str],
            sets: dict[str, dict]) -> list[str]:
    """Items the manifest does not list: anything inside a chosen set's
    folder, and files (not folders) outside every set's folder."""
    by_root = {root: name for name, root in roots.items()}
    extra: list[str] = []
    for parent, dirs, files in os.walk(restored, followlinks=False):
        dirs.sort()
        inside = next((r for r in by_root
                       if parent == r or parent.startswith(r + _SEP)), None)
        if inside is not None and by_root[inside] not in chosen:
            dirs[:] = []                  # another set's folder: not checked
            continue
        for item in sorted(dirs + files):
            full = os.path.join(parent, item)
            if inside is not None:
                if _rel(full, inside) not in sets[by_root[inside]]["entries"]:
                    extra.append(_rel(full, restored))
            elif full not in by_root and (item in files or os.path.islink(full)):
                extra.append(_rel(full, restored))
    return extra


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("restored", type=Path)
    ap.add_argument("--repo", help="Check only this repository's items.")
    ap.add_argument("--no-metadata", action="store_true",
                    help="Skip modes, symlinks and hardlinks (a folder "
                         "copied off Windows keeps none of them).")
    args = ap.parse_args()

    if not args.manifest.is_file():
        return _error(f"no manifest at {args.manifest}")
    if not args.restored.is_dir():
        return _error(f"no folder at {args.restored}: check the path")
    try:
        sets = _read_manifest(args.manifest)
    except ValueError as exc:
        return _error(str(exc))
    if not any(s.get("entries") for s in sets.values()):
        return _error(f"the manifest {args.manifest} lists no files")
    chosen = {name: s for name, s in sets.items()
              if args.repo is None or name == args.repo}
    if not chosen:
        return _error(
            f"the manifest has no files of repository {args.repo!r}; "
            f"it has: {', '.join(sorted(sets))}"
        )

    restored = os.fsencode(args.restored)
    if not any(files for _p, _d, files in os.walk(restored)):
        return _error(f"the folder {args.restored} holds no files")
    roots: dict[str, bytes] = {}
    for name, s in sets.items():
        top = {rel.split("/", 1)[0] for rel in s["entries"]}
        root = _find_set_root(restored, name, top)
        if root is not None:
            roots[name] = root
    if not any(name in roots for name in chosen):
        n = sum(len(s["entries"]) for s in chosen.values())
        return _error(
            f"none of the {n} expected items is in {args.restored}: "
            "is it the right folder?"
        )

    checker = Checker(metadata=not args.no_metadata)
    for name, s in sorted(chosen.items()):
        checker.check_set(name, roots.get(name), s["entries"], s.get("absent", []))
    extra = _extras(restored, roots, set(chosen), sets)
    for rel in extra:
        print(f"EXTRA    {_show(rel)}  (not in the manifest)")

    print(f"{checker.checked - len(checker.bad)}/{checker.checked} "
          "items restored intact")
    if args.no_metadata:
        print("Not checked (--no-metadata): modes, hardlinks and "
              f"{checker.skipped_links} symlink(s).")
    if extra:
        print(f"{len(extra)} extra item(s) not in the manifest (listed above)")
    return 0 if not checker.bad else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
