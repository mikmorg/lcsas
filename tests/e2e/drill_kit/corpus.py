"""The drill corpus (#834): fixed, non-personal test files for each backup set.

The corpus is built in two states, one per backup (snapshot):

- ``build_corpus``  writes the first state;
- ``change_corpus`` turns it into the second state: one file edited, one
  deleted, one renamed, one mode changed and a few files added.

``scan_tree`` reads a corpus folder back (``lstat``, bytes names, links not
followed) into the manifest entries ``verify_restore.py`` checks.  The kit
writes the manifest from the source tree this way, so it always says what
was really backed up.

Two profiles:

- ``windows`` (the default; the #476 and #474 drills restore both sets on
  Windows): only names NTFS can store, no symlinks, and the long path stays
  short enough for a Windows restore folder under MAX_PATH (tier 1 copes
  with longer ones since #619; tier 2 and Explorer may not).
- ``posix`` (Linux and macOS): everything in ``windows``, plus names with a
  quote, a backslash, a tab, a colon, a byte that is not UTF-8, a trailing
  dot or space, a case-only twin and a reserved DOS name; a path over 260
  characters; and relative (up and across), absolute and dangling symlinks.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import stat
from pathlib import Path

PROFILES = ("windows", "posix")
MANIFEST_FORMAT = "lcsas-drill-manifest/1"   # verify_restore.py reads it

MiB = 1024 * 1024

# (repository, text files, bulk files, bulk file size).  The first backup:
# about 450 MiB with the big file, so the first burn session is one CD700
# data disc (each holds about 498 MiB of packs after the default 100 MB
# metadata reserve).
CORPUS: tuple[tuple[str, int, int, int], ...] = (
    ("photos", 4, 30, 6 * MiB),
    ("documents", 6, 28, 6 * MiB),
)
# (repository, bulk files added before the second backup, their size).
# About 130 MiB: the second burn session is one more data disc.  Two data
# discs in all, as the owner decided for the drill archive (2026-11-02).
ADDED: tuple[tuple[str, int, int], ...] = (
    ("photos", 12, 6 * MiB),
    ("documents", 10, 6 * MiB),
)
# One file above 100 MB, in one set (#834).  Generated, never committed.
BIG_FILE = ("photos", "videos/home movie (test data).bin", 101 * MiB)

# A Windows restore lands in %USERPROFILE%\Documents\restored[-<set>]\<set>\:
# about 50 characters before the set's own path for a short user name.
# Windows tier 1 restores longer paths since #619, but tier 2
# (rustic-static) and programs like Explorer may not, so the Windows
# set's longest path stays at most this long: the whole path stays
# under 240.
WINDOWS_MAX_SET_PATH = 180
# The posix set's long path is longer than this (Windows MAX_PATH).
POSIX_MIN_LONG_PATH = 260

DIR_MODE = 0o755
FILE_MODE = 0o644
READ_ONLY_DIR_MODE = 0o555
READ_ONLY_FILE_MODE = 0o444
PRIVATE_MODE = 0o600
PRIVATE_MODE_AFTER = 0o640

ABOUT = "ABOUT THIS TEST DATA.txt"

# Names NTFS can store: spaces, accents, an emoji, other scripts, and the
# punctuation cmd.exe treats specially but NTFS allows.
COMMON_NAMES = (
    "name with spaces.txt",
    "café crème, résumé – naïve.txt",
    "Ångström Øre Ünïcödé.txt",
    "emoji 📷🎉 photo.txt",
    "日本語 ファイル.txt",
    "it's (copy) [1] {x} & 100% #1 !.txt",
    "UPPER and lower.TXT",
)
# Linux/macOS only: NTFS cannot store these (or Windows reserves them).
POSIX_NAMES: tuple[bytes, ...] = (
    b'a "double quote".txt',
    b"back\\slash.txt",
    b"tab\there.txt",
    b"colon: star* question? <angle> pipe|.txt",
    b"trailing dot.",
    b"trailing space ",
    b"case.txt",
    b"CASE.txt",
    b"CON.txt",
    # Latin-1 "caf\xe9": not valid UTF-8.  macOS cannot store it either
    # (recovery/docs/FORMAT.txt, PATH SAFETY), so it is a Linux-only check.
    b"latin-1 caf\xe9.txt",
)
NON_UTF8_NAME = b"latin-1 caf\xe9.txt"

_WORDS = (
    "archive", "disc", "drill", "restore", "sample", "record", "family",
    "garden", "harbour", "lantern", "meadow", "orchard", "pebble", "river",
    "season", "summer", "timber", "valley", "window", "winter", "letter",
    "notebook", "journal", "recipe", "ledger", "map", "album", "page",
    "chapter", "note",
)


def _text(rng: random.Random, title: str) -> bytes:
    lines = [
        title,
        "",
        "This file is TEST DATA for an LCSAS restore drill.",
        "It contains no personal information.",
        "",
    ]
    for _ in range(40):
        lines.append(" ".join(rng.choice(_WORDS) for _ in range(12)).capitalize() + ".")
    return ("\n".join(lines) + "\n").encode()


def _mkdir(path: Path) -> None:
    """Make *path* and any missing parent, each with DIR_MODE (not the
    umask), so the manifest is the same on every machine."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for p in reversed(missing):
        p.mkdir()
        os.chmod(p, DIR_MODE)


def _write(path: Path, data: bytes, mode: int = FILE_MODE) -> None:
    _mkdir(path.parent)
    path.write_bytes(data)
    os.chmod(path, mode)


def _write_random(path: Path, seed: str, size: int) -> None:
    """Incompressible, deterministic bytes, written 1 MiB at a time."""
    _mkdir(path.parent)
    rng = random.Random(seed)
    with open(path, "wb") as f:
        left = size
        while left:
            n = min(left, MiB)
            f.write(rng.randbytes(n))
            left -= n
    os.chmod(path, FILE_MODE)


def _long_path_parts(profile: str, repo: str) -> list[str]:
    """Folders + file name of the long path, relative to the set's root."""
    if profile == "posix":
        # Six 50-character folders: over 300 characters in all, every part
        # well under the 255-byte name limit.
        folders = [f"long folder name number {i} " + "x" * 24 for i in range(1, 7)]
        return ["long path", *folders, "file at the end of a long path.txt"]
    # Fill up to WINDOWS_MAX_SET_PATH, counting "<repo>/" in front.
    leaf = "file at the end of a long path.txt"
    parts = ["long path"]
    used = len(repo) + 1 + len("long path/") + len(leaf)
    i = 1
    while used + 40 <= WINDOWS_MAX_SET_PATH:
        part = f"long folder name {i} " + "y" * 19
        parts.append(part)
        used += len(part) + 1
        i += 1
    return [*parts, leaf]


def build_corpus(root: Path, seed: str, profile: str = "windows") -> None:
    """Write the first state of every set under *root*/<repo>/."""
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}; use one of {PROFILES}")
    _mkdir(root)
    for repo, n_text, n_bulk, bulk_size in CORPUS:
        d = root / repo
        _mkdir(d)
        _write(d / ABOUT, (
            f"Backup set '{repo}' of an LCSAS restore drill.\n"
            "Every file here is TEST DATA. Nothing is personal.\n"
        ).encode())
        stem = "album-notes" if repo == "photos" else "letter"
        for i in range(1, n_text + 1):
            name = f"{stem}-{i:02d}.txt"
            _write(d / name, _text(random.Random(f"{seed}:{repo}:text:{i}"),
                                   f"{repo} {name}"))
        bulk = "scan" if repo == "photos" else "document"
        for i in range(1, n_bulk + 1):
            # Incompressible, so the packs really fill the discs.
            _write_random(d / f"{bulk}-{i:03d}.dat", f"{seed}:{repo}:bulk:{i}",
                          bulk_size)
        if BIG_FILE[0] == repo:
            _write_random(d / BIG_FILE[1], f"{seed}:{repo}:big", BIG_FILE[2])
        _special_items(d, f"{seed}:{repo}", profile, repo)


def _special_items(d: Path, seed: str, profile: str, repo: str) -> None:
    def small(tag: str) -> bytes:
        return _text(random.Random(f"{seed}:{tag}"), tag)

    names = d / "names"
    _mkdir(names)
    for name in COMMON_NAMES:
        _write(names / name, small(name))
    _write(d / "Fotos da família" / "praia ☀ verão.txt", small("praia"))
    if profile == "posix":
        for raw in POSIX_NAMES:
            _write(names / os.fsdecode(raw), small(raw.hex()))

    deep = d / "deep"
    for i in range(1, 21):
        deep = deep / f"d{i:02d}"
    _write(deep / "bottom of the deep tree.txt", small("deep"))

    long_path = d.joinpath(*_long_path_parts(profile, repo))
    _write(long_path, small("long"))

    _mkdir(d / "empty folder")
    _mkdir(d / "empty folder" / "also empty")
    _write(d / "empty file.txt", b"")
    _write(d / "private (mode 600).txt", small("private"), PRIVATE_MODE)

    original = d / "hardlinks" / "original.txt"
    _write(original, small("hardlink"))
    os.link(original, d / "hardlinks" / "second name for the same file.txt")

    ro = d / "read-only folder"
    _mkdir(ro)
    _write(ro / "inside a read-only folder.txt", small("ro-1"))
    _write(ro / "read-only file.txt", small("ro-2"), READ_ONLY_FILE_MODE)
    os.chmod(ro, READ_ONLY_DIR_MODE)

    _write(d / "renamed" / "old name.txt", small("renamed"))

    if profile == "posix":
        links = d / "links"
        _write(links / "b" / "f", small("link target"))
        _mkdir(links / "a")
        os.symlink("../b/f", links / "a" / "up")             # up and across
        os.symlink("../names", links / "to a folder")        # to a folder
        os.symlink("/lcsas-drill/absolute target (test data)",
                   links / "absolute")                       # absolute
        os.symlink("no such file.txt", links / "dangling")   # dangling
        # A target that is not valid UTF-8 (rustic stores linktarget_raw).
        os.symlink(b"latin-1 caf\xe9.txt", os.fsencode(links / "non-utf8 target"))


def change_corpus(root: Path, seed: str) -> None:
    """Turn the first state into the second, for the second backup.

    In every set: one file edited, one deleted, one renamed, one mode
    changed, a text file and a few bulk files added.  A restore of the first
    snapshot fails verify_restore.py, so "latest" is really checked.
    """
    for repo, *_ in CORPUS:
        d = root / repo
        stem = "album-notes" if repo == "photos" else "letter"
        edited = d / f"{stem}-01.txt"
        os.chmod(edited, FILE_MODE)
        with open(edited, "ab") as f:
            f.write(b"\nEdited after the first backup.\n")
        (d / f"{stem}-02.txt").unlink()
        (d / "renamed" / "old name.txt").rename(d / "renamed" / "new name.txt")
        os.chmod(d / "private (mode 600).txt", PRIVATE_MODE_AFTER)
        _write(d / "added after the first backup.txt",
               _text(random.Random(f"{seed}:{repo}:added"), "added"))
        n_added, size = next((n, s) for r, n, s in ADDED if r == repo)
        bulk = "scan" if repo == "photos" else "document"
        for i in range(1, n_added + 1):
            _write_random(d / f"{bulk}-added-{i:03d}.dat",
                          f"{seed}:{repo}:added:{i}", size)


def _sha256(path: bytes) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(MiB), b""):
            h.update(chunk)
    return h.hexdigest()


def scan_tree(set_root: Path) -> dict[str, dict]:
    """Every item under *set_root*, by its path relative to it.

    Paths and link targets are the file system's bytes, decoded with
    ``os.fsdecode`` (a byte that is not UTF-8 becomes a lone surrogate,
    which JSON keeps).  Hardlinked files share a ``hardlink`` group number.
    """
    entries: dict[str, dict] = {}
    inodes: dict[tuple[int, int], list[str]] = {}
    base = os.fsencode(set_root)
    for top, dirs, files in os.walk(base, followlinks=False):
        dirs.sort()
        # A symlink to a folder is listed in dirs (and not entered).
        for name in sorted(dirs + files):
            full = os.path.join(top, name)
            rel = os.fsdecode(os.path.relpath(full, base))
            st = os.lstat(full)
            if stat.S_ISDIR(st.st_mode):
                entries[rel] = {"type": "dir", "mode": stat.S_IMODE(st.st_mode)}
            elif stat.S_ISLNK(st.st_mode):
                entries[rel] = {"type": "symlink",
                                "target": os.fsdecode(os.readlink(full))}
            elif stat.S_ISREG(st.st_mode):
                entries[rel] = {"type": "file", "mode": stat.S_IMODE(st.st_mode),
                                "size": st.st_size, "sha256": _sha256(full)}
                if st.st_nlink > 1:
                    inodes.setdefault((st.st_dev, st.st_ino), []).append(rel)
            elif stat.S_ISFIFO(st.st_mode):
                entries[rel] = {"type": "fifo", "mode": stat.S_IMODE(st.st_mode)}
            else:
                raise ValueError(f"unexpected item in the corpus: {full!r}")
    for group, paths in enumerate(sorted(sorted(p) for p in inodes.values()), 1):
        for rel in paths:
            entries[rel]["hardlink"] = group
    return dict(sorted(entries.items()))


def scan_sets(root: Path) -> dict[str, dict[str, dict]]:
    """scan_tree of every set under *root*."""
    return {repo: scan_tree(root / repo) for repo, *_ in CORPUS}


def make_manifest(root: Path, first: dict[str, dict[str, dict]], *,
                  profile: str, seed: str) -> dict:
    """The manifest of the corpus as it is now (the newest backup).

    *first* is ``scan_sets`` of the first backup's state: what it had and
    the newest one does not is listed as "absent", so a restore of the
    older snapshot fails the check.
    """
    sets = {}
    for repo, now in scan_sets(root).items():
        sets[repo] = {"entries": now,
                      "absent": sorted(set(first[repo]) - set(now))}
    return {"format": MANIFEST_FORMAT, "profile": profile, "seed": seed,
            "sets": sets}


def write_manifest(path: Path, manifest: dict) -> None:
    # ensure_ascii: a byte that is not UTF-8 stays a \udcXX escape.
    path.write_text(json.dumps(manifest, indent=1, ensure_ascii=True) + "\n")


def is_ntfs_safe(name: str) -> bool:
    """True if NTFS (and Windows) can store this one path component."""
    if not name or name in (".", ".."):
        return False
    if any(ch in '\\/:*?"<>|' or ord(ch) < 32 or 0xD800 <= ord(ch) <= 0xDFFF
           for ch in name):
        return False
    if name[-1] in ". ":
        return False
    stem = name.split(".", 1)[0].upper().rstrip()
    reserved = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} \
        | {f"LPT{i}" for i in range(1, 10)}
    return stem not in reserved
