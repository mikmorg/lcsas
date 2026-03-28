"""Helpers for the two-level hash-prefix pack file layout.

Both Rustic mirrors (written by rustic itself) and LCSAS staging/restore
caches use the same two-level directory structure::

    <root>/
    └── data/
        ├── ab/
        │   └── abcdef1234...   (full SHA-256 hex)
        └── cd/
            └── cdef5678...

The functions here centralise the path logic so it is not duplicated
across staging, restore, and scanner modules.
"""

from __future__ import annotations

from pathlib import Path

# Metadata sub-directories present inside each per-repo metadata tree.
# Defined here so all modules refer to the same names rather than
# hardcoding strings independently.
METADATA_SUBDIRS: tuple[str, ...] = ("index", "snapshots", "keys")


def find_pack_file(data_dir: Path, sha256: str) -> Path | None:
    """Locate a pack file under *data_dir* by its SHA-256 hash.

    Checks the two-level layout (``data/<prefix>/<hash>``) first, then
    falls back to the flat layout (``data/<hash>``).

    Args:
        data_dir: The ``data/`` directory of a mirror or staging tree.
        sha256: Full SHA-256 hex string of the pack to find.

    Returns:
        Absolute path to the pack file, or *None* if not present.
    """
    if len(sha256) >= 2:
        prefixed = data_dir / sha256[:2] / sha256
        if prefixed.is_file():
            return prefixed

    flat = data_dir / sha256
    if flat.is_file():
        return flat

    return None


def pack_dest_path(data_dir: Path, sha256: str) -> Path:
    """Return the canonical destination path for a pack in *data_dir*.

    Always uses the two-level layout for newly staged/cached packs so
    that the resulting tree is compatible with rustic/restic 0.14+.

    Args:
        data_dir: The ``data/`` directory of a staging tree or cache.
        sha256: Full SHA-256 hex string of the pack.

    Returns:
        Absolute destination path (parent directory may not exist yet).
    """
    if len(sha256) >= 2:
        return data_dir / sha256[:2] / sha256
    return data_dir / sha256
