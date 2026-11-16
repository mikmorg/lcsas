#!/usr/bin/env python3
"""Check a drill restore against the kit's manifest.

usage: verify_restore.py <manifest.sha256> <restored dir> [--repo NAME]

The manifest lists "<sha256>  <repo>/<file>".  A restore may nest the files
under extra directories (the restore target, a snapshot path), so each entry
is matched by its "<repo>/<file>" tail.  Prints one line per missing or
differing file, lists files that are in the folder but not in the manifest,
then a summary.  Exits 0 only when every entry (of --repo, if given) is
present with the right hash.

Exits 2, with a message, when there is nothing to compare: the folder does
not exist or holds no files, the manifest (or --repo) selects no entries, or
none of the selected files is in the folder.  "0/0 files restored intact"
is never a pass (#833).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

EXIT_MISMATCH = 1
EXIT_NOTHING_COMPARED = 2


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _error(message: str) -> int:
    print(f"ERROR: {message}", file=sys.stderr)
    print("Nothing was checked.  This is not a pass.", file=sys.stderr)
    return EXIT_NOTHING_COMPARED


def _read_manifest(path: Path) -> dict[str, str]:
    """All entries, "<repo>/<file>" -> sha256."""
    entries: dict[str, str] = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        digest, sep, rel = line.partition("  ")
        if not sep or not rel:
            raise ValueError(f"{path}: line {number} is not '<sha256>  <path>'")
        entries[rel] = digest
    return entries


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("restored", type=Path)
    ap.add_argument("--repo", help="Check only this repository's files.")
    args = ap.parse_args()

    if not args.manifest.is_file():
        return _error(f"no manifest at {args.manifest}")
    if not args.restored.is_dir():
        return _error(f"no folder at {args.restored}: check the path")
    try:
        manifest = _read_manifest(args.manifest)
    except ValueError as exc:
        return _error(str(exc))

    expected = {
        rel: digest for rel, digest in manifest.items()
        if args.repo is None or rel.split("/", 1)[0] == args.repo
    }
    if not manifest:
        return _error(f"the manifest {args.manifest} lists no files")
    if not expected:
        repos = sorted({rel.split("/", 1)[0] for rel in manifest})
        return _error(
            f"the manifest has no files of repository {args.repo!r}; "
            f"it has: {', '.join(repos)}"
        )

    found: dict[str, Path] = {}
    extra: list[str] = []
    any_file = False
    for path in sorted(args.restored.rglob("*")):
        if not path.is_file():
            continue
        any_file = True
        parts = path.relative_to(args.restored).parts
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else ""
        if tail in manifest:
            found.setdefault(tail, path)
        else:
            extra.append("/".join(parts))
    if not any_file:
        return _error(f"the folder {args.restored} holds no files")
    if not any(rel in found for rel in expected):
        return _error(
            f"none of the {len(expected)} expected files is in "
            f"{args.restored}: is it the right folder?"
        )

    bad = 0
    for rel, digest in sorted(expected.items()):
        path = found.get(rel)
        if path is None:
            print(f"MISSING  {rel}")
            bad += 1
        elif _sha256(path) != digest:
            print(f"DIFFERS  {rel}")
            bad += 1
    for rel in extra:
        print(f"EXTRA    {rel}  (not in the manifest)")
    print(f"{len(expected) - bad}/{len(expected)} files restored intact")
    if extra:
        print(f"{len(extra)} extra file(s) not in the manifest (listed above)")
    return 0 if bad == 0 else EXIT_MISMATCH


if __name__ == "__main__":
    raise SystemExit(main())
