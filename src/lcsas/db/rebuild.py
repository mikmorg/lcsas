"""Catalog rebuild — merge disc-embedded catalogs into a new master database."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

_logger = logging.getLogger(__name__)

# SQLite 3.33.0 introduced UPDATE...FROM syntax (Sept 2020)
_MIN_SQLITE_VERSION = (3, 33, 0)


def _check_sqlite_version() -> tuple[int, int, int]:
    """Return the SQLite version as a (major, minor, patch) tuple."""
    version_str = sqlite3.sqlite_version
    parts = version_str.split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (int(parts[0]), int(parts[1]), 0)


@dataclass
class RebuildResult:
    """Summary of a catalog rebuild operation."""

    discs_processed: int = 0
    discs_skipped: int = 0
    repositories_merged: int = 0
    volumes_merged: int = 0
    packs_merged: int = 0
    snapshots_merged: int = 0
    locations_merged: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _merge_one_disc(
    target: sqlite3.Connection,
    source_db: Path,
) -> dict[str, int]:
    """Attach *source_db* and merge its data into *target*.

    Uses INSERT OR IGNORE on natural-key columns so records are only added
    when they do not already exist.  Status conflicts are resolved by
    preferring the less-destroyed state: if the target has DESTROYED and
    the source has VERIFIED, update to VERIFIED.

    Returns a dict mapping table name → rows inserted.
    """
    counts: dict[str, int] = {}

    # Use a safe alias to avoid conflicts with other attached DBs.
    alias = "src"
    target.execute(f"ATTACH DATABASE ? AS {alias}", (str(source_db),))
    try:
        # 1. repositories — keyed on repo_id
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO repositories (repo_id, name, mirror_path,
                encryption_key_id, created_at)
            SELECT repo_id, name, mirror_path, encryption_key_id, created_at
            FROM {alias}.repositories
            """
        )
        counts["repositories"] = cur.rowcount

        # 2. locations — keyed on name
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO locations (name, description, created_at)
            SELECT name, description, created_at
            FROM {alias}.locations
            """
        )
        counts["locations"] = cur.rowcount

        # 3. packs — keyed on sha256
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO packs (sha256, size_bytes, repo_id,
                is_pruned, created_at)
            SELECT sha256, size_bytes, repo_id, is_pruned, created_at
            FROM {alias}.packs
            """
        )
        counts["packs"] = cur.rowcount

        # 4. volumes — keyed on uuid.
        #    Insert new volumes; status conflicts are resolved below.
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO volumes
                (label, uuid, media_type, capacity_bytes, used_bytes,
                 location, status, created_at, closed_at, verified_at)
            SELECT label, uuid, media_type, capacity_bytes, used_bytes,
                   location, status, created_at, closed_at, verified_at
            FROM {alias}.volumes
            """
        )
        counts["volumes"] = cur.rowcount

        # Resolve status conflicts: prefer the highest-quality status.
        # Rank: VERIFIED (best) > BURNED > CONSOLIDATING > BURNING > STAGING
        #       > DEPRECATED > DESTROYED (worst)
        # If the source has a higher-quality status than the existing row,
        # update the existing row to the source's status.
        #
        # Note: Implemented as explicit loop for SQLite < 3.33 compatibility
        # (UPDATE...FROM was added in SQLite 3.33.0).
        for row in target.execute(
            f"""
            SELECT volumes.volume_id, src_v.status
            FROM volumes
            JOIN {alias}.volumes src_v ON src_v.uuid = volumes.uuid
            WHERE (
                CASE volumes.status
                    WHEN 'VERIFIED'      THEN 6
                    WHEN 'BURNED'        THEN 5
                    WHEN 'CONSOLIDATING' THEN 4
                    WHEN 'BURNING'       THEN 3
                    WHEN 'STAGING'       THEN 2
                    WHEN 'DEPRECATED'    THEN 1
                    WHEN 'DESTROYED'     THEN 0
                    ELSE 0
                END
            ) < (
                CASE src_v.status
                    WHEN 'VERIFIED'      THEN 6
                    WHEN 'BURNED'        THEN 5
                    WHEN 'CONSOLIDATING' THEN 4
                    WHEN 'BURNING'       THEN 3
                    WHEN 'STAGING'       THEN 2
                    WHEN 'DEPRECATED'    THEN 1
                    WHEN 'DESTROYED'     THEN 0
                    ELSE 0
                END
            )
            """
        ):
            target.execute(
                "UPDATE volumes SET status = ? WHERE volume_id = ?",
                (row[1], row[0]),
            )

        # 5. snapshots — keyed on snapshot_id
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO snapshots
                (snapshot_id, repo_id, hostname, timestamp,
                 paths, tags, description)
            SELECT snapshot_id, repo_id, hostname, timestamp,
                   paths, tags, description
            FROM {alias}.snapshots
            """
        )
        counts["snapshots"] = cur.rowcount

        # 6. volume_packs — keyed on (volume_id, pack_id).
        #    We must translate IDs from the source DB since auto-increment IDs
        #    differ between databases.  Join on natural keys (uuid, sha256).
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO volume_packs (volume_id, pack_id)
            SELECT v.volume_id, p.pack_id
            FROM {alias}.volume_packs svp
            JOIN {alias}.volumes sv ON sv.volume_id = svp.volume_id
            JOIN {alias}.packs  sp ON sp.pack_id   = svp.pack_id
            JOIN volumes v ON v.uuid   = sv.uuid
            JOIN packs   p ON p.sha256 = sp.sha256
            """
        )
        counts["volume_packs"] = cur.rowcount

        # 7. volume_copies — keyed on (volume_id, location)
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO volume_copies
                (volume_id, location, status, burn_date, notes, iso_sha256,
                 last_verified_at, media_serial)
            SELECT v.volume_id, svc.location, svc.status, svc.burn_date,
                   svc.notes, svc.iso_sha256, svc.last_verified_at,
                   svc.media_serial
            FROM {alias}.volume_copies svc
            JOIN {alias}.volumes sv ON sv.volume_id = svc.volume_id
            JOIN volumes v ON v.uuid = sv.uuid
            """
        )
        counts["volume_copies"] = cur.rowcount

        target.commit()

    finally:
        target.execute(f"DETACH DATABASE {alias}")

    return counts


def rebuild_catalog(
    disc_paths: list[Path],
    output_db: Path,
) -> RebuildResult:
    """Merge holographic catalogs from *disc_paths* into *output_db*.

    Each entry in *disc_paths* should be a mounted LCSAS disc directory
    containing a ``catalog.db`` file.  The output DB is created if it does
    not already exist (useful for building a fresh master catalog from scratch),
    or data is merged into the existing file.

    Conflict resolution:
    - Records with the same natural key are kept as-is (INSERT OR IGNORE).
    - Volume status prefers the "most alive" version (VERIFIED > DESTROYED).

    Args:
        disc_paths: List of mounted disc root directories.
        output_db: Path to the new (or existing) master catalog.

    Returns:
        :class:`RebuildResult` with merge statistics and any errors.
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all

    result = RebuildResult()

    # Ensure the output DB is initialised with the current schema.
    conn = get_connection(output_db)
    create_all(conn)

    for disc_path in disc_paths:
        catalog_db = disc_path / "catalog.db"
        if not catalog_db.is_file():
            _logger.warning(
                "No catalog.db found at %s — skipping.", disc_path
            )
            result.discs_skipped += 1
            result.errors.append(f"No catalog.db at {disc_path}")
            continue

        _logger.info("Merging catalog from: %s", disc_path)
        try:
            counts = _merge_one_disc(conn, catalog_db)
        except Exception as exc:
            _logger.error("Failed to merge %s: %s", disc_path, exc)
            result.discs_skipped += 1
            result.errors.append(f"{disc_path}: {exc}")
            continue

        result.discs_processed += 1
        result.repositories_merged += counts.get("repositories", 0)
        result.volumes_merged += counts.get("volumes", 0)
        result.packs_merged += counts.get("packs", 0)
        result.snapshots_merged += counts.get("snapshots", 0)
        result.locations_merged += counts.get("locations", 0)

        _logger.info(
            "  → %d repositories, %d volumes, %d packs, %d snapshots merged",
            counts.get("repositories", 0),
            counts.get("volumes", 0),
            counts.get("packs", 0),
            counts.get("snapshots", 0),
        )

    conn.close()
    return result
