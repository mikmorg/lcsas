"""Holographic metadata injection for staging directories.

Every optical disc includes a complete copy of all repository metadata
and the archive catalog, enabling recovery from any single disc.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path
from typing import Sequence

from lcsas.config.settings import LCSASConfig
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

IMPORTANT: If these instructions are confusing, take ALL the discs plus
the encryption key to a computer professional.  Any Linux system
administrator or IT professional should be able to follow these steps.
See START_HERE.txt on this disc for more context about this archive.

WHAT YOU NEED TO RESTORE
------------------------

1. This disc (and possibly others — check catalog.db for the full list)
2. Your encryption key file (NOT stored on any disc for security)
3. The LCSAS meta-volume disc (contains all required tools), OR:
   - rustic (https://rustic.cli.rs/) or restic (https://restic.net/)
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

1. Extract ISOs (multiple options — use whichever works):
       # Option A: mount directly (requires root)
       sudo mount -o loop,ro VOLUME.iso /mnt/disc
       # Option B: use 7z
       7z x VOLUME.iso -o/tmp/vol1/
       # Option C: use xorriso
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
  START_HERE.txt   Plain-language description of this archive
  RESTORE_INSTRUCTIONS.txt  This file

For the encryption/pack file format specification, see
docs/RESTIC_FORMAT_SPEC.md on the LCSAS meta-volume disc.
"""
        path = self._root / "RESTORE_INSTRUCTIONS.txt"
        path.write_text(text)

    def write_start_here(self, config: LCSASConfig) -> None:
        """Write a plain-language START_HERE.txt to the staging root.

        This file is written in simple English for a non-technical
        person (e.g. a family member who finds these discs after the
        archivist's death).

        Args:
            config: LCSAS configuration with survivability fields.
        """
        owner = config.archive_owner or "the person who created this archive"
        description = config.archive_description or (
            "digital files backed up using LCSAS (Linux Cold Storage Archival Suite)"
        )
        key_hints = config.key_storage_hints or (
            "The archive creator should have stored the key in a safe place.\n"
            "  Check for a USB drive, paper printout, or password manager entry\n"
            "  labeled 'LCSAS', 'backup key', or 'archive key'."
        )
        contact = config.technical_contact or (
            "Any Linux system administrator or IT professional should be\n"
            "  able to follow the instructions in RESTORE_INSTRUCTIONS.txt."
        )

        # Build repo section if available
        repo_lines = ""
        if config.repositories:
            repo_names = ", ".join(sorted(config.repositories.keys()))
            repo_lines = f"\n  Repositories on these discs: {repo_names}\n"

        text = textwrap.dedent(f"""\
            ╔══════════════════════════════════════════════════════════╗
            ║                    START HERE                           ║
            ╚══════════════════════════════════════════════════════════╝

            WHAT ARE THESE DISCS?
            ---------------------

              These discs contain backup copies of digital files created
              by {owner}.

              Contents: {description}
            {repo_lines}
            HOW TO GET YOUR FILES BACK
            --------------------------

              1. You need an ENCRYPTION KEY to unlock the data on these discs.
                 The key is NOT on any disc (for security).

                 Where to find the key:
                 {key_hints}

              2. You need ALL of the discs (or at least the ones containing
                 the files you want).

              3. You need a computer running Linux, or someone who can help
                 you use one.

            IMPORTANT: If this is confusing, take ALL the discs and the
            encryption key to a computer professional.  They do NOT need
            to understand this system — the instructions are on the discs.

              Who can help:
              {contact}

            WARNING: WITHOUT THE ENCRYPTION KEY, THE DATA ON THESE DISCS
            CANNOT BE RECOVERED.  EVER.  BY ANYONE.  Keep the key safe.

            WHAT TO DO NEXT
            ----------------

              - Look for a disc labeled "META" — it contains the recovery
                tools and a script called restore.sh that automates
                everything.

              - If you cannot find the meta-volume disc, the file
                RESTORE_INSTRUCTIONS.txt on this disc has step-by-step
                manual recovery instructions.

            DISC CARE
            ---------

              - Store discs vertically (like books), not stacked flat.
              - Keep in a cool, dry, dark place.
              - Handle by the edges — do not touch the data surface.
              - Blu-ray discs (especially M-Disc) can last 100+ years
                with proper storage.
        """)

        path = self._root / "START_HERE.txt"
        path.write_text(text)

    def write_key_info(self, config: LCSASConfig) -> None:
        """Write KEY_INFO.txt mapping repositories to their key requirements.

        Args:
            config: LCSAS configuration with repository definitions.
        """
        lines = [
            "KEY INFORMATION",
            "===============",
            "",
            "This file lists each backup repository on these discs and",
            "which encryption key is needed to access it.",
            "",
        ]

        if not config.repositories:
            lines.append("No repositories are configured.")
            lines.append("")
        else:
            for name, repo in sorted(config.repositories.items()):
                lines.append(f"Repository: {name}")
                if repo.encryption_key_id:
                    lines.append(f"  Key ID: {repo.encryption_key_id}")
                if repo.password_file:
                    lines.append(
                        f"  Key file name: {repo.password_file.name}"
                    )
                else:
                    lines.append("  Key file: (not specified in config)")
                lines.append("")

        if config.key_storage_hints:
            lines.append("WHERE TO FIND THE KEY(S)")
            lines.append("-----------------------")
            lines.append(config.key_storage_hints)
            lines.append("")

        lines.append("NOTE: Each repository requires its own encryption key.")
        lines.append("If you have only one key file, it likely works for all")
        lines.append("repositories (common setup).")
        lines.append("")

        path = self._root / "KEY_INFO.txt"
        path.write_text("\n".join(lines))
