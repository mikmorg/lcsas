"""Scan a Rustic local mirror for pack files on disk."""

from __future__ import annotations

import contextlib
import logging
import os
import re
from pathlib import Path

_logger = logging.getLogger(__name__)

# Pack filenames in Rustic repos are 64-char lowercase hex (SHA-256).
_PACK_NAME_RE = re.compile(r"^[0-9a-f]{64}$")


def _register_pack(
    packs: dict[str, int],
    name: str,
    size: int,
    parent: Path,
) -> None:
    """Validate and register a candidate pack file entry."""
    if not _PACK_NAME_RE.match(name):
        _logger.debug(
            "Skipping non-pack file in data dir: %s/%s", parent, name
        )
        return
    if size == 0:
        _logger.warning(
            "Pack file %s/%s has zero bytes — skipping (possibly incomplete write).",
            parent, name,
        )
        return
    packs[name] = size


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

    try:
        top_entries = list(os.scandir(data_dir))
    except PermissionError as exc:
        _logger.warning("Cannot scan %s: %s", data_dir, exc)
        return packs

    for entry in top_entries:
        try:
            if entry.is_file() and not entry.name.startswith("."):
                # Flat layout: data/abcdef1234...
                _register_pack(packs, entry.name, entry.stat().st_size, data_dir)
            elif entry.is_dir():
                # Two-level layout: data/ab/abcdef1234...
                subdir = Path(entry.path)
                try:
                    sub_entries = list(os.scandir(subdir))
                except PermissionError as exc:
                    _logger.warning("Cannot scan %s: %s", subdir, exc)
                    continue
                for sub_entry in sub_entries:
                    if sub_entry.is_file() and not sub_entry.name.startswith("."):
                        try:
                            _register_pack(
                                packs, sub_entry.name,
                                sub_entry.stat().st_size, subdir,
                            )
                        except OSError as exc:
                            _logger.warning("Cannot access pack file %s: %s", sub_entry.path, exc)
        except OSError as exc:
            _logger.warning("Cannot process entry %s: %s", entry.path, exc)

    return packs
