"""Holographic metadata injection for staging directories.

Every optical disc includes a complete copy of all repository metadata
and the archive catalog, enabling recovery from any single disc.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Sequence

from lcsas.db.models import Pack, Volume
from lcsas.utils.fs import copy_file, copy_tree, ensure_dir

# Repository subdirectories that constitute "hot" metadata
_METADATA_DIRS = ["index", "snapshots", "keys"]
_METADATA_FILES = ["config"]


class HolographicInjector:
    """Injects repository metadata and catalog into a staging directory."""

    def __init__(self, staging_root: Path) -> None:
        self._root = staging_root
        self._metadata_dir = staging_root / "metadata"

    def inject_metadata(
        self,
        mirror_paths: dict[str, Path],
    ) -> None:
        """Copy repository metadata from each mirror into the staging tree.

        Args:
            mirror_paths: Dict of {repo_id: mirror_root_path}.
                Each mirror_root should contain index/, snapshots/, keys/, config.
        """
        for repo_id, mirror_root in mirror_paths.items():
            repo_meta_dir = self._metadata_dir / repo_id
            ensure_dir(repo_meta_dir)

            # Copy metadata directories
            for subdir_name in _METADATA_DIRS:
                src = mirror_root / subdir_name
                if src.is_dir():
                    copy_tree(src, repo_meta_dir / subdir_name)

            # Copy metadata files
            for fname in _METADATA_FILES:
                src = mirror_root / fname
                if src.is_file():
                    copy_file(src, repo_meta_dir / fname)

    def inject_catalog(self, db_path: Path) -> None:
        """Copy the SQLite archive catalog to the staging root."""
        dst = self._root / "catalog.db"
        copy_file(db_path, dst)

    def write_volume_info(
        self,
        volume: Volume,
        packs: Sequence[Pack] = (),
    ) -> None:
        """Write a self-describing JSON file to the staging root.

        This allows any disc to identify itself and its contents
        without consulting the database.

        Args:
            volume: The volume record.
            packs: Optional sequence of packs on this volume.  When
                provided, the JSON includes ``pack_count``,
                ``total_bytes``, ``repositories``, and
                ``sha256_manifest``.
        """
        info: dict = {
            "uuid": volume.uuid,
            "label": volume.label,
            "media_type": volume.media_type,
            "capacity_bytes": volume.capacity_bytes,
            "status": volume.status,
            "created_at": volume.created_at,
        }

        if packs:
            info["pack_count"] = len(packs)
            info["total_bytes"] = sum(p.size_bytes for p in packs)
            info["repositories"] = sorted(
                {p.repo_id for p in packs if p.repo_id}
            )
            info["sha256_manifest"] = sorted(p.sha256 for p in packs)

        info_path = self._root / "volume_info.json"
        with open(info_path, "w") as f:
            json.dump(info, f, indent=2)

    def write_restore_instructions(self) -> None:
        """Write a human-readable RESTORE_INSTRUCTIONS.txt to the staging root.

        This file is placed on every data disc so that anyone who finds
        a disc alone can understand what it contains and what is needed
        to recover data from it.
        """
        text = """\
LCSAS Data Volume — Restore Instructions
=========================================

This disc is part of an LCSAS (Linux Cold Storage Archival Suite) archive.
It contains encrypted, deduplicated backup pack files and a snapshot of
the archive catalog database.

WHAT YOU NEED TO RESTORE
------------------------

1. This disc (and possibly others — check catalog.db for the full list)
2. Your encryption key file (NOT stored on any disc for security)
3. The LCSAS meta-volume disc (contains all required tools), OR:
   - rustic (https://rustic.cli.rs/) or restic (https://restic.net/)
   - xorriso (https://www.gnu.org/software/xorriso/)
   - Python 3.10+ and the LCSAS source code

HOW TO RESTORE (with meta-volume)
----------------------------------

If you have the LCSAS meta-volume disc:

    cd /path/to/meta-volume
    ./restore.sh --key /path/to/keyfile \\
                 --isos /path/to/iso-directory \\
                 --target /path/to/output

HOW TO RESTORE (manual)
------------------------

1. Extract ISOs:
       xorriso -indev VOLUME.iso -osirrox on -extract / /tmp/vol1/

2. Inspect catalog:
       sqlite3 /tmp/vol1/catalog.db "SELECT label FROM volumes"

3. Copy pack files into a rustic-compatible cache layout:
       for f in /tmp/vol*/data/*; do
           sha=$(basename "$f")
           prefix=${sha:0:2}
           mkdir -p /tmp/cache/data/$prefix
           cp "$f" /tmp/cache/data/$prefix/$sha
       done

4. Copy metadata (index, snapshots, keys, config) from any volume:
       cp -r /tmp/vol1/metadata/REPO_NAME/* /tmp/cache/

5. Restore:
       rustic restore latest -r /tmp/cache --password-file keyfile --target /output

DISC CONTENTS
--------------

  data/            Pack files (encrypted, content-addressable by SHA-256)
  metadata/        Repository index, snapshot, and key files
  catalog.db       Archive catalog (SQLite — cumulative as of this volume)
  volume_info.json Machine-readable volume identity and manifest
  RESTORE_INSTRUCTIONS.txt  This file

For more details, see the LCSAS project: https://github.com/your-org/lcsas
"""
        path = self._root / "RESTORE_INSTRUCTIONS.txt"
        path.write_text(text)
