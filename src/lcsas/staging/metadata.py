"""Holographic metadata injection for staging directories.

Every optical disc includes a complete copy of all repository metadata
and the archive catalog, enabling recovery from any single disc.
"""

from __future__ import annotations

import json
import os
import shutil
import textwrap
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from lcsas.config.settings import LCSASConfig
from lcsas.db.models import Pack, Volume
from lcsas.restore.standalone_builder import build_standalone
from lcsas.utils.fs import copy_file, copy_tree, ensure_dir
from lcsas.utils.pack_layout import METADATA_SUBDIRS

# Repository subdirectories that constitute "hot" metadata
_METADATA_DIRS = list(METADATA_SUBDIRS)
_METADATA_FILES = ["config"]

# Empirical lower-bound reserve for a single-repo fixture's holographic
# injection on TEST_TINY-class media.  The injected payload is dominated
# by the SQLite catalog skeleton + per-repo Rustic index/snapshots/keys
# + ISO 9660 filesystem overhead — together that's about 650-700 KB even
# for a 1-repo / few-packs fixture.  Production deployments don't care
# (the LCSASConfig default of 100 MB swamps it), but test fixtures that
# materialize an ISO on the 1 MB TEST_TINY media MUST budget at least
# this much in ``metadata_reserve_bytes`` or the staging directory will
# overflow capacity.  Bump if the injector grows.
MIN_HOLOGRAPHIC_RESERVE_BYTES = 700_000


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
        info: dict[str, Any] = {
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
        with open(info_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

    def write_lcsas_source(self) -> None:
        """Copy the LCSAS restore subpackage source onto the disc.

        Bundles ``src/lcsas/restore/`` and ``src/lcsas/utils/`` so that
        a technically capable person can inspect, patch, or re-run the
        restore logic without any pre-installed packages.  If the source
        tree is not found at install time (e.g. editable install moved)
        this step is skipped with a warning rather than failing the burn.
        """
        # Locate the installed lcsas package source.
        lcsas_pkg = Path(__file__).parent.parent  # src/lcsas/
        dst_root = self._root / "lcsas_src"

        for subpkg in ("restore", "utils", "db"):
            src = lcsas_pkg / subpkg
            if src.is_dir():
                dst = dst_root / subpkg
                if not dst.exists():
                    shutil.copytree(str(src), str(dst), symlinks=True)
            else:
                import logging
                logging.getLogger(__name__).warning(
                    "LCSAS source subpackage not found at %s — "
                    "source will not be bundled on this disc.",
                    src,
                )

    def write_standalone_restorer(self) -> None:
        """Write a self-contained pure-Python restorer to the staging root.

        This file is generated from ``_aes_pure.py`` and
        ``restic_fallback.py`` and has zero dependencies on the
        ``lcsas`` package.  It provides a last-resort restore path
        that works with nothing but Python 3.10+ stdlib.
        """
        text = build_standalone()
        path = self._root / "standalone_restorer.py"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

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

If you have the LCSAS meta-volume disc, insert it and mount it,
then run the interactive restore script.  It will prompt you for
the repository name (if multi-tenant), the encryption password, and
will pause for each data disc as it needs them.

    sudo mount /dev/sr0 /mnt
    sh /mnt/restore.sh ~/restored/ latest

When prompted, eject the current disc, insert the named data disc,
and press Enter.  Repeat until you see "RESTORE COMPLETE".

If you prefer a password file over the interactive prompt, set
LCSAS_PWFILE=/path/to/key.txt before invoking restore.sh.

HOW TO RESTORE (pure Python — no native binaries needed)
---------------------------------------------------------

If no rustic/restic binary works (wrong architecture, missing libs):

  1. Extract ISOs as described below.
  2. Assemble a cache directory (copy metadata + packs — see manual steps).
  3. Run the standalone restorer included on this disc:

       python3 standalone_restorer.py --repo /tmp/cache \\
           --password-file /path/to/keyfile --target /path/to/output

  This script requires only Python 3.10+ standard library.
  For zstd-compressed repositories, also install: pip install zstandard

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
  KEY_INFO.txt     Which encryption key(s) are needed for each repository
  CONFIG_SUMMARY.txt  Archive configuration snapshot
  DISC_CARE.txt    Storage and handling guidance for optical media
  standalone_restorer.py  Pure-Python restore script (no binaries needed)
  RESTORE_INSTRUCTIONS.txt  This file

For the encryption/pack file format specification, see
docs/RESTIC_FORMAT_SPEC.md on the LCSAS meta-volume disc.
"""
        path = self._root / "RESTORE_INSTRUCTIONS.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

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
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())

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
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush()
            os.fsync(f.fileno())

    def write_config_summary(self, config: LCSASConfig) -> None:
        """Write a sanitized config summary to the staging root.

        Includes repository names, media type, and survivability fields
        but strips filesystem paths (which are host-specific and useless
        on a standalone disc).

        Args:
            config: LCSAS configuration.
        """
        lines = [
            "LCSAS CONFIGURATION SUMMARY",
            "===========================",
            "",
            f"Media type:      {config.default_media_type.name}",
            f"ECC redundancy:  {config.default_ecc_redundancy_pct}%",
            f"Label prefix:    {config.label_prefix}",
            "",
        ]

        if config.archive_owner:
            lines.append(f"Archive owner:       {config.archive_owner}")
        if config.archive_description:
            lines.append(f"Archive description: {config.archive_description}")
        if config.technical_contact:
            lines.append(f"Technical contact:   {config.technical_contact}")
        if config.archive_owner or config.archive_description:
            lines.append("")

        if config.repositories:
            lines.append("REPOSITORIES")
            lines.append("------------")
            for name, repo in sorted(config.repositories.items()):
                lines.append(f"  {name}")
                if repo.encryption_key_id:
                    lines.append(f"    Key ID: {repo.encryption_key_id}")
            lines.append("")

        lines.append("NOTE: Filesystem paths are omitted because they are")
        lines.append("specific to the original system and not useful for")
        lines.append("restoration. See RESTORE_INSTRUCTIONS.txt instead.")
        lines.append("")

        path = self._root / "CONFIG_SUMMARY.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            f.flush()
            os.fsync(f.fileno())

    def write_disc_care(self) -> None:
        """Write DISC_CARE.txt with media storage guidance.

        This standalone file provides detailed disc care instructions
        beyond the brief summary in START_HERE.txt.
        """
        text = """\
DISC CARE & STORAGE GUIDE
=========================

These discs contain irreplaceable backup data.  Proper storage
significantly extends their readable life.

HANDLING
--------

  - Hold discs by the edges or the center hole only.
  - NEVER touch the shiny data surface (bottom of disc).
  - Do not bend, flex, or stack heavy objects on discs.
  - Do not write on the label side with a ballpoint pen
    (pressure can damage the data layer).  Use only soft
    felt-tip markers designed for optical media.

STORAGE
-------

  - Store discs VERTICALLY (like books on a shelf), not
    stacked flat.  Flat stacking puts pressure on surfaces.
  - Use a quality disc binder with individual sleeves, or
    standard jewel cases.
  - Avoid paper or cardboard sleeves for long-term storage
    (they can scratch and trap moisture).

ENVIRONMENT
-----------

  - Temperature: 15-25 C (60-77 F) ideal.  Avoid extremes.
  - Humidity: 30-50% relative humidity.  Too dry causes
    brittleness; too humid encourages mold and corrosion.
  - Light: Store in a DARK place.  UV light degrades organic
    dyes used in recordable Blu-ray and DVD media.
  - Avoid: attics (heat), basements (moisture), garages
    (temperature swings), direct sunlight, near windows.

MEDIA LONGEVITY
---------------

  - M-DISC (Millenniata): Rated for 1000+ years.  Uses an
    inorganic data layer that does not degrade like organic
    dye.  Best choice for archival.
  - Standard BD-R HTL: ~50-100 years with proper storage.
  - Standard BD-R LTH: ~10-30 years (organic dye, less stable).
  - DVD+R / DVD-R: ~10-50 years depending on dye quality.

  M-DISC is strongly recommended for archival purposes.

PERIODIC VERIFICATION
---------------------

  Even with proper storage, verify discs periodically:

  - Every 2-5 years: spot-check a few discs
  - Every 5-10 years: full verify of all discs
  - If ANY disc shows read errors, consider re-burning ALL
    data to fresh media (media in the same batch may be
    degrading similarly)

  Use the LCSAS verify command:

    lcsas verify --isos /path/to/disc/images/

  Or use dvdisaster to check ECC integrity:

    dvdisaster -i /dev/sr0 -t

DRIVE AVAILABILITY
------------------

  As optical drives disappear from consumer hardware:

  - Keep at least one USB Blu-ray drive in your disc binder
    or storage location.
  - USB external BD drives are widely available and affordable.
  - Standard USB interface ensures future compatibility.
  - Internal SATA BD drives with a USB-SATA adapter also work.
"""
        path = self._root / "DISC_CARE.txt"
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
