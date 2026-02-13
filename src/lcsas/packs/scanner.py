"""Scan a Rustic local mirror for pack files on disk."""

from __future__ import annotations

import os
from pathlib import Path


def scan_mirror_packs(mirror_path: Path) -> dict[str, int]:
    """Scan a Rustic repository's data directory for pack files.

    Handles both flat layout (data/HASH) and two-level hash-prefix
    layout (data/ab/abcdef...). Returns {sha256_filename: size_bytes}.

    Args:
        mirror_path: Root of the Rustic repository (contains data/ subdir).

    Returns:
        Dict mapping pack filename (the SHA-256 hash) to file size in bytes.
    """
    data_dir = mirror_path / "data"
    if not data_dir.is_dir():
        return {}

    packs: dict[str, int] = {}

    for entry in os.scandir(data_dir):
        if entry.is_file() and not entry.name.startswith("."):
            # Flat layout: data/abcdef1234...
            packs[entry.name] = entry.stat().st_size
        elif entry.is_dir():
            # Two-level layout: data/ab/abcdef1234...
            subdir = Path(entry.path)
            for sub_entry in os.scandir(subdir):
                if sub_entry.is_file() and not sub_entry.name.startswith("."):
                    packs[sub_entry.name] = sub_entry.stat().st_size

    return packs
