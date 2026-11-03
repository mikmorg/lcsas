#!/usr/bin/env python3
"""Check a drill restore against the kit's manifest.

usage: verify_restore.py <manifest.sha256> <restored dir> [--repo NAME]

The manifest lists "<sha256>  <repo>/<file>".  A restore may nest the files
under extra directories (the restore target, a snapshot path), so each entry
is matched by its "<repo>/<file>" tail.  Prints one line per missing or
differing file, then a summary; exits 0 only when every entry (of --repo,
if given) is present with the right hash.
"""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("manifest", type=Path)
    ap.add_argument("restored", type=Path)
    ap.add_argument("--repo", help="Check only this repository's files.")
    args = ap.parse_args()

    expected: dict[str, str] = {}
    for line in args.manifest.read_text().splitlines():
        digest, rel = line.split("  ", 1)
        if args.repo is None or rel.split("/", 1)[0] == args.repo:
            expected[rel] = digest

    found: dict[str, Path] = {}
    for path in args.restored.rglob("*"):
        if path.is_file():
            parts = path.relative_to(args.restored).parts
            if len(parts) >= 2:
                found.setdefault("/".join(parts[-2:]), path)

    bad = 0
    for rel, digest in sorted(expected.items()):
        path = found.get(rel)
        if path is None:
            print(f"MISSING  {rel}")
            bad += 1
        elif _sha256(path) != digest:
            print(f"DIFFERS  {rel}")
            bad += 1
    print(f"{len(expected) - bad}/{len(expected)} files restored intact")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
