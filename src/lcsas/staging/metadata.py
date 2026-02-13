"""Holographic metadata injection for staging directories.

Every optical disc includes a complete copy of all repository metadata
and the archive catalog, enabling recovery from any single disc.
"""

from __future__ import annotations

import json
from pathlib import Path

from lcsas.db.models import Volume
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

    def write_volume_info(self, volume: Volume) -> None:
        """Write a self-describing JSON file to the staging root.

        This allows any disc to identify itself and its contents
        without consulting the database.
        """
        info = {
            "uuid": volume.uuid,
            "label": volume.label,
            "media_type": volume.media_type,
            "capacity_bytes": volume.capacity_bytes,
            "status": volume.status,
            "created_at": volume.created_at,
        }
        info_path = self._root / "volume_info.json"
        with open(info_path, "w") as f:
            json.dump(info, f, indent=2)
