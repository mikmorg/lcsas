"""Catalog rebuild — merge disc-embedded catalogs into a new master database."""

from __future__ import annotations

import logging
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

_logger = logging.getLogger(__name__)

# SQLite 3.33.0 introduced UPDATE...FROM syntax (Sept 2020)
_MIN_SQLITE_VERSION = (3, 33, 0)

# Liveness rank for volume statuses.  Recency — not rank — decides which
# status wins a merge conflict (FMA-06); the rank is used only to DETECT
# the resurrection hazard (a stale catalog claiming a volume is more
# alive than the freshest one records) so it can be surfaced as a warning.
_STATUS_RANK = {
    "VERIFIED": 6,
    "BURNED": 5,
    "CONSOLIDATING": 4,
    "BURNING": 3,
    "STAGING": 2,
    "DEPRECATED": 1,
    "DESTROYED": 0,
}


def _check_sqlite_version() -> tuple[int, int, int]:
    """Return the SQLite version as a (major, minor, patch) tuple."""
    version_str = sqlite3.sqlite_version
    parts = version_str.split(".")
    try:
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    except (IndexError, ValueError):
        return (int(parts[0]), int(parts[1]), 0)


def _parse_timestamp(value: object) -> float | None:
    """Best-effort: catalog DATETIME text → epoch seconds (None on failure).

    SQLite's CURRENT_TIMESTAMP writes naive UTC ("YYYY-MM-DD HH:MM:SS");
    naive values are interpreted as UTC.
    """
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.timestamp()


def _source_freshness(catalog_db: Path) -> float:
    """Freshness ordinal for a disc catalog (bigger = newer).

    Max of the catalog file's mtime (preserved by Rock Ridge on the ISO)
    and ``MAX(volumes.created_at)`` inside the DB — the row-derived value
    guards against copies that mangled the mtime (FMA-06).
    """
    freshness = catalog_db.stat().st_mtime
    try:
        # Percent-encode so mount paths containing '?', '#', or spaces
        # survive SQLite's URI parsing.
        uri = "file:" + urllib.parse.quote(str(catalog_db)) + "?mode=ro"
        conn = sqlite3.connect(uri, uri=True)
        try:
            row = conn.execute("SELECT MAX(created_at) FROM volumes").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # Unreadable/corrupt source — the merge itself will report it.
        return freshness
    row_ts = _parse_timestamp(row[0] if row else None)
    if row_ts is not None and row_ts > freshness:
        freshness = row_ts
    return freshness


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
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _merge_one_disc(
    target: sqlite3.Connection,
    source_db: Path,
    *,
    source_freshness: float = 0.0,
    status_freshness: dict[str, float] | None = None,
    warnings: list[str] | None = None,
) -> dict[str, int]:
    """Attach *source_db* and merge its data into *target*.

    Uses INSERT OR IGNORE on natural-key columns so records are only added
    when they do not already exist.  Volume-status conflicts are resolved
    by recency (FMA-06): the freshest catalog that mentions a volume owns
    its status — including downgrades to DEPRECATED/DESTROYED.

    *status_freshness* maps volume uuid → freshness of the catalog that
    last set its status; it is shared across all merges of one rebuild
    run.  When the source is staler than that record, its status is
    ignored — and if the stale status ranks *more alive* (the
    resurrection hazard) a warning is appended to *warnings*.

    Returns a dict mapping table name → rows inserted (plus the special
    key ``is_pruned_conflicts``: packs where a staler source disagreed
    with the kept ``is_pruned`` flag).
    """
    counts: dict[str, int] = {}
    if status_freshness is None:
        status_freshness = {}
    if warnings is None:
        warnings = []

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

        # 3. packs — keyed on sha256.  rebuild_catalog() merges discs
        #    newest-first, so the row already in the target was written by
        #    a fresher catalog and INSERT OR IGNORE keeps the freshest
        #    view.  Count is_pruned disagreements from this (staler)
        #    source so the rebuild can emit one summary warning.
        row = target.execute(
            f"""
            SELECT COUNT(*)
            FROM {alias}.packs sp
            JOIN packs p ON p.sha256 = sp.sha256
            WHERE p.is_pruned != sp.is_pruned
            """
        ).fetchone()
        counts["is_pruned_conflicts"] = int(row[0])

        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO packs (sha256, size_bytes, repo_id,
                is_pruned, created_at)
            SELECT sha256, size_bytes, repo_id, is_pruned, created_at
            FROM {alias}.packs
            """
        )
        counts["packs"] = cur.rowcount

        # 4. volumes — keyed on uuid.  Status conflicts are resolved by
        #    RECENCY, not liveness rank: discs burned before a volume was
        #    deprecated/destroyed carry catalogs that still record it as
        #    VERIFIED, and rank-based merging silently resurrected such
        #    volumes (FMA-06).
        conflicts = target.execute(
            f"""
            SELECT volumes.volume_id, volumes.uuid, volumes.label,
                   volumes.status, src_v.status
            FROM volumes
            JOIN {alias}.volumes src_v ON src_v.uuid = volumes.uuid
            """
        ).fetchall()
        conflict_uuids = {row[1] for row in conflicts}

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

        # Volumes this source introduced: it owns their status (until a
        # fresher source says otherwise — cannot happen with newest-first
        # ordering, but _merge_one_disc does not assume it).
        for row in target.execute(f"SELECT uuid FROM {alias}.volumes"):
            if row[0] not in conflict_uuids:
                status_freshness[row[0]] = source_freshness

        for volume_id, uuid_, label, tgt_status, src_status in conflicts:
            last = status_freshness.get(uuid_)
            if last is None or source_freshness > last:
                # Freshest view so far — its status wins, downgrades to
                # DEPRECATED/DESTROYED included.
                if src_status != tgt_status:
                    target.execute(
                        "UPDATE volumes SET status = ? WHERE volume_id = ?",
                        (src_status, volume_id),
                    )
                status_freshness[uuid_] = source_freshness
            elif (
                _STATUS_RANK.get(src_status, 0)
                > _STATUS_RANK.get(tgt_status, 0)
            ):
                # Resurrection hazard: a stale catalog claims the volume
                # is MORE alive than the freshest one records.  Keep the
                # fresh status and warn loudly.
                warnings.append(
                    f"volume {label}: an older disc catalog records "
                    f"{src_status} but a newer one records {tgt_status} — "
                    f"keeping {tgt_status}. If you physically hold this "
                    f"disc, verify it with 'lcsas verify {label} --disc'."
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

        # 7. volume_copies — keyed on (volume_id, location).
        #    iso_size_bytes arrived in schema v8 (FMA-03); disc catalogs
        #    burned before that lack the column, so select it only when
        #    the source actually has it (holographic snapshots are
        #    frozen forever — they can never be migrated in place).
        src_copy_cols = {
            r[1] for r in target.execute(
                f"PRAGMA {alias}.table_info(volume_copies)"
            ).fetchall()
        }
        size_expr = (
            "svc.iso_size_bytes" if "iso_size_bytes" in src_copy_cols
            else "NULL"
        )
        cur = target.execute(
            f"""
            INSERT OR IGNORE INTO volume_copies
                (volume_id, location, status, burn_date, notes, iso_sha256,
                 iso_size_bytes, last_verified_at, media_serial)
            SELECT v.volume_id, svc.location, svc.status, svc.burn_date,
                   svc.notes, svc.iso_sha256, {size_expr},
                   svc.last_verified_at, svc.media_serial
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

    Conflict resolution (recency-aware, FMA-06):
    - Discs are merged NEWEST-first.  Freshness per disc is the max of the
      ``catalog.db`` mtime and ``MAX(volumes.created_at)`` inside it, so
      the merge result does not depend on the order discs are listed.
    - Records with the same natural key keep the freshest catalog's view
      (newest-first ordering + INSERT OR IGNORE).
    - Volume status follows the freshest catalog that mentions the volume,
      including downgrades to DEPRECATED/DESTROYED.  A stale disc claiming
      a volume is more alive than the freshest record (the resurrection
      hazard) is reported in :attr:`RebuildResult.warnings`.

    Args:
        disc_paths: List of mounted disc root directories.
        output_db: Path to the new (or existing) master catalog.

    Returns:
        :class:`RebuildResult` with merge statistics, warnings, and errors.
    """
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema

    result = RebuildResult()

    # Ensure the output DB is initialised with the current schema.
    # Source disc catalogs are ATTACHed read-only below and are never
    # migrated — only the output catalog goes through ensure_schema.
    conn = get_connection(output_db)
    ensure_schema(conn)

    # Volumes already present in the output catalog get a freshness
    # baseline from its own rows (the file mtime is useless here — we
    # just touched it via ensure_schema), so a genuinely fresher disc
    # can still update a stale pre-existing master.
    status_freshness: dict[str, float] = {}
    baseline = _parse_timestamp(
        conn.execute("SELECT MAX(created_at) FROM volumes").fetchone()[0]
    )
    if baseline is not None:
        for row in conn.execute("SELECT uuid FROM volumes"):
            status_freshness[row[0]] = baseline

    # Sort newest-first.  Path is a deterministic tie-breaker; discs with
    # no catalog.db sort last (they only produce a skip error).
    ordered: list[tuple[Path, Path | None, float]] = []
    for disc_path in disc_paths:
        catalog_db = disc_path / "catalog.db"
        if catalog_db.is_file():
            ordered.append(
                (disc_path, catalog_db, _source_freshness(catalog_db))
            )
        else:
            ordered.append((disc_path, None, float("-inf")))
    ordered.sort(key=lambda entry: (-entry[2], str(entry[0])))

    is_pruned_conflicts = 0
    for disc_path, source_db, freshness in ordered:
        if source_db is None:
            _logger.warning(
                "No catalog.db found at %s — skipping.", disc_path
            )
            result.discs_skipped += 1
            result.errors.append(f"No catalog.db at {disc_path}")
            continue

        _logger.info("Merging catalog from: %s", disc_path)
        try:
            counts = _merge_one_disc(
                conn,
                source_db,
                source_freshness=freshness,
                status_freshness=status_freshness,
                warnings=result.warnings,
            )
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
        is_pruned_conflicts += counts.get("is_pruned_conflicts", 0)

        _logger.info(
            "  → %d repositories, %d volumes, %d packs, %d snapshots merged",
            counts.get("repositories", 0),
            counts.get("volumes", 0),
            counts.get("packs", 0),
            counts.get("snapshots", 0),
        )

    if is_pruned_conflicts:
        result.warnings.append(
            f"{is_pruned_conflicts} pack record(s) in staler disc catalogs "
            "disagreed on is_pruned — kept the freshest catalog's view "
            "(discs are merged newest-first)."
        )

    conn.close()
    return result
