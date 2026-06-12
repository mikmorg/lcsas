"""Complex cross-table queries for the LCSAS catalog."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any

from lcsas.db.models import Pack, Snapshot, Volume
from lcsas.db.packs import _row_to_pack
from lcsas.db.snapshots import _row_to_snapshot
from lcsas.db.volumes import _row_to_volume

_logger = logging.getLogger(__name__)

# Conservative batch limit – stays below SQLite's 999-variable limit on old builds.
_SQLITE_BATCH = 900

# FMA-01: a pack is "archived" only when it is linked to a volume in one of
# these statuses — i.e. a disc that physically exists.  STAGING/BURNING
# volumes are an intent, not a copy.  CONSOLIDATING is durable because it is
# only entered from VERIFIED (the disc exists).
DURABLE_VOLUME_STATUSES = ("BURNED", "VERIFIED", "CONSOLIDATING")

# SQL literal list built once from the single source of truth above.
_DURABLE_SQL = "(" + ", ".join(f"'{s}'" for s in DURABLE_VOLUME_STATUSES) + ")"

# EXISTS-condition shared by the archived/unarchived queries below.
_HAS_DURABLE_LINK = f"""EXISTS (
    SELECT 1 FROM volume_packs vp
    JOIN volumes v ON v.volume_id = vp.volume_id
    WHERE vp.pack_id = p.pack_id
      AND v.status IN {_DURABLE_SQL}
)"""


def get_unarchived_packs(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
) -> list[Pack]:
    """Return non-pruned packs with no link to a durable (burned) volume.

    These are packs sitting on the Local Mirror that need to be burned.
    Packs linked only to STAGING/BURNING volumes count as unarchived:
    a volume that was never burned is not a copy, so its packs must
    reappear in the default ``lcsas stage`` pool (FMA-01).  A pack with
    at least one durable link stays archived (no double-burning).
    """
    if repo_id:
        rows = conn.execute(
            f"""SELECT p.* FROM packs p
               WHERE p.is_pruned = 0
                 AND p.repo_id = ?
                 AND NOT {_HAS_DURABLE_LINK}
               ORDER BY p.created_at""",
            (repo_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""SELECT p.* FROM packs p
               WHERE p.is_pruned = 0
                 AND NOT {_HAS_DURABLE_LINK}
               ORDER BY p.created_at"""
        ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_total_unarchived_bytes(
    conn: sqlite3.Connection,
    repo_id: str | None = None,
) -> int:
    """Return total bytes of unarchived, non-pruned packs.

    Uses the same durable-volume semantics as :func:`get_unarchived_packs`.
    """
    if repo_id:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(p.size_bytes), 0) as total
               FROM packs p
               WHERE p.is_pruned = 0
                 AND p.repo_id = ?
                 AND NOT {_HAS_DURABLE_LINK}""",
            (repo_id,),
        ).fetchone()
    else:
        row = conn.execute(
            f"""SELECT COALESCE(SUM(p.size_bytes), 0) as total
               FROM packs p
               WHERE p.is_pruned = 0
                 AND NOT {_HAS_DURABLE_LINK}"""
        ).fetchone()
    if row is None:
        raise RuntimeError("get_total_unarchived_bytes: aggregate query returned no row")
    return int(row[0])


def get_packs_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[Pack]:
    """Return all packs on a specific volume."""
    rows = conn.execute(
        """SELECT p.* FROM packs p
           JOIN volume_packs vp ON p.pack_id = vp.pack_id
           WHERE vp.volume_id = ?
           ORDER BY p.sha256""",
        (volume_id,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_volumes_for_pack(
    conn: sqlite3.Connection,
    pack_id: int,
) -> list[Volume]:
    """Return all volumes containing a specific pack (redundancy check)."""
    rows = conn.execute(
        """SELECT v.* FROM volumes v
           JOIN volume_packs vp ON v.volume_id = vp.volume_id
           WHERE vp.pack_id = ?
           ORDER BY v.label""",
        (pack_id,),
    ).fetchall()
    return [_row_to_volume(r) for r in rows]


def get_pick_list(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
    preferred_location: str = "",
) -> dict[str, list[Pack]]:
    """Generate a restore 'pick list': map volume labels to needed packs.

    Given a list of required pack SHA-256 hashes (from a restore dry-run),
    returns a dict of {volume_label: [Pack, ...]} telling the user which
    discs to retrieve.

    Prefers non-DEPRECATED/DESTROYED volumes. If a pack exists on multiple
    volumes, prefers durable (BURNED/VERIFIED/CONSOLIDATING) volumes over
    never-burned STAGING/BURNING ones, then volumes at *preferred_location*
    (if specified) to minimise disc-swapping across locations. Falls back
    to alphabetical order.

    STAGING/BURNING volumes are deliberately NOT excluded: every on-disc
    holographic catalog lists its own volume as STAGING (the catalog is
    copied at staging time), so excluding them would break every restore
    that runs against a disc-rebuilt catalog (FMA-01/FMA-10).  Use
    :func:`get_unconfirmed_volume_labels` to warn about selected volumes
    that have no record of ever being burned.

    Args:
        conn: DB connection.
        pack_sha256_list: SHA-256 hashes of required packs.
        preferred_location: Optional storage location to prefer (e.g.
            ``"Home_Shelf"``).  Volumes at this location are chosen
            over volumes elsewhere when both carry the same pack.
    """
    if not pack_sha256_list:
        return {}

    if preferred_location:
        # Warn if the preferred location doesn't exist in the DB so the
        # user gets feedback rather than silently falling back to any volume.
        row = conn.execute(
            "SELECT 1 FROM locations WHERE name = ? LIMIT 1",
            (preferred_location,),
        ).fetchone()
        if row is None:
            _logger.warning(
                "get_pick_list: preferred_location '%s' not found in catalog — "
                "falling back to alphabetical volume order",
                preferred_location,
            )

    # Deduplicate: each pack assigned to one volume only.
    # Process in batches to avoid SQLite variable limit.
    seen_packs: set[str] = set()
    result: dict[str, list[Pack]] = {}

    for batch_start in range(0, len(pack_sha256_list), _SQLITE_BATCH):
        batch = pack_sha256_list[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)

        # Order by: durable volumes first (a disc that exists always beats
        # a never-burned STAGING claim), then preferred location, then
        # alphabetically.
        if preferred_location:
            rows = conn.execute(
                f"""SELECT p.*, v.volume_id, v.label as vol_label,
                           v.status as vol_status, v.location as vol_location
                    FROM packs p
                    JOIN volume_packs vp ON p.pack_id = vp.pack_id
                    JOIN volumes v ON vp.volume_id = v.volume_id
                    WHERE p.sha256 IN ({placeholders})
                      AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                    ORDER BY (CASE WHEN v.status IN {_DURABLE_SQL}
                                   THEN 0 ELSE 1 END),
                             (CASE WHEN v.location = ? THEN 0 ELSE 1 END),
                             v.label""",
                [*batch, preferred_location],
            ).fetchall()
        else:
            rows = conn.execute(
                f"""SELECT p.*, v.volume_id, v.label as vol_label,
                           v.status as vol_status
                    FROM packs p
                    JOIN volume_packs vp ON p.pack_id = vp.pack_id
                    JOIN volumes v ON vp.volume_id = v.volume_id
                    WHERE p.sha256 IN ({placeholders})
                      AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                    ORDER BY (CASE WHEN v.status IN {_DURABLE_SQL}
                                   THEN 0 ELSE 1 END),
                             v.label""",
                batch,
            ).fetchall()

        for row in rows:
            pack = _row_to_pack(row)
            if pack.sha256 in seen_packs:
                continue
            seen_packs.add(pack.sha256)
            vol_label = row["vol_label"]
            result.setdefault(vol_label, []).append(pack)

    return result


def get_pick_list_with_alternates(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
    preferred_location: str = "",
) -> dict[str, dict[str, Any]]:
    """Generate a pick list with alternate volumes for each pack.

    Returns a dict keyed by pack SHA-256:
        {sha256: {"pack": Pack, "primary_label": str,
                  "primary_volume_id": int, "alternates": [str, ...]}}

    The primary volume is chosen by: durable (BURNED/VERIFIED/
    CONSOLIDATING) before never-burned STAGING/BURNING, then preferred
    location, then VERIFIED before BURNED, then alphabetical label.
    Alternates are the remaining volumes that also hold the pack.

    STAGING volumes are kept as last-resort candidates rather than
    excluded — see :func:`get_pick_list` for why (FMA-01/FMA-10).
    """
    if not pack_sha256_list:
        return {}

    # Group by pack sha256: first row = primary, rest = alternates.
    # Process in batches to avoid SQLite variable limit.
    result: dict[str, dict[str, Any]] = {}

    for batch_start in range(0, len(pack_sha256_list), _SQLITE_BATCH):
        batch = pack_sha256_list[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)

        params: list[Any] = list(batch)
        location_order = ""
        if preferred_location:
            location_order = "(CASE WHEN v.location = ? THEN 0 ELSE 1 END),"
            params.append(preferred_location)

        rows = conn.execute(
            f"""SELECT p.*, v.volume_id, v.label AS vol_label,
                       v.status AS vol_status, v.location AS vol_location
                FROM packs p
                JOIN volume_packs vp ON p.pack_id = vp.pack_id
                JOIN volumes v ON vp.volume_id = v.volume_id
                WHERE p.sha256 IN ({placeholders})
                  AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                ORDER BY (CASE WHEN v.status IN {_DURABLE_SQL}
                               THEN 0 ELSE 1 END),
                         {location_order}
                         (CASE WHEN v.status = 'VERIFIED' THEN 0
                               WHEN v.status = 'BURNED' THEN 1
                               ELSE 2 END),
                         v.label""",
            params,
        ).fetchall()

        for row in rows:
            pack = _row_to_pack(row)
            vol_label = row["vol_label"]
            vol_id = row["volume_id"]

            if pack.sha256 not in result:
                result[pack.sha256] = {
                    "pack": pack,
                    "primary_label": vol_label,
                    "primary_volume_id": vol_id,
                    "alternates": [],
                }
            else:
                result[pack.sha256]["alternates"].append(vol_label)

    return result


def get_unconfirmed_volume_labels(
    conn: sqlite3.Connection,
    labels: list[str],
) -> set[str]:
    """Return the subset of *labels* with no record of ever being burned.

    A volume is "unconfirmed" when its status is STAGING/BURNING and it
    has zero ``volume_copies`` rows: the catalog has no evidence the disc
    was ever written.  Pick lists still offer such volumes (an on-disc
    holographic catalog always lists its own volume as STAGING), but the
    restore planner must warn that the disc may never have existed
    (FMA-01).
    """
    if not labels:
        return set()

    unconfirmed: set[str] = set()
    for batch_start in range(0, len(labels), _SQLITE_BATCH):
        batch = labels[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT v.label FROM volumes v
                WHERE v.label IN ({placeholders})
                  AND v.status IN ('STAGING', 'BURNING')
                  AND NOT EXISTS (
                      SELECT 1 FROM volume_copies vc
                      WHERE vc.volume_id = v.volume_id
                  )""",
            batch,
        ).fetchall()
        unconfirmed.update(row["label"] for row in rows)
    return unconfirmed


def get_missing_packs(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
) -> list[str]:
    """Return SHA-256 hashes from the input list that have no accessible volume.

    A pack is considered missing if:
    - It is not in the catalog at all, or
    - It has no volume assignment, or
    - All of its volumes are DEPRECATED or DESTROYED (physically gone).

    Packs that exist only on DEPRECATED/DESTROYED volumes are included here
    so callers treat them as unrestorable from normal storage.  Use
    :func:`get_deprecated_only_packs` to identify which deprecated discs
    might still be physically recoverable.
    """
    if not pack_sha256_list:
        return []

    missing: list[str] = []

    for batch_start in range(0, len(pack_sha256_list), _SQLITE_BATCH):
        batch = pack_sha256_list[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)

        # Packs that exist in the catalog
        archived = {r["sha256"] for r in conn.execute(
            f"SELECT sha256 FROM packs WHERE sha256 IN ({placeholders})",
            batch,
        ).fetchall()}

        # Packs not even in the DB
        for h in batch:
            if h not in archived:
                missing.append(h)

        # Packs in DB but with no active (non-DEPRECATED/DESTROYED) volume
        no_active_volume = conn.execute(
            f"""SELECT p.sha256 FROM packs p
                WHERE p.sha256 IN ({placeholders})
                  AND NOT EXISTS (
                      SELECT 1 FROM volume_packs vp
                      JOIN volumes v ON v.volume_id = vp.volume_id
                      WHERE vp.pack_id = p.pack_id
                        AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                  )""",
            batch,
        ).fetchall()
        for row in no_active_volume:
            if row["sha256"] not in missing:
                missing.append(row["sha256"])

    return missing


def get_deprecated_only_packs(
    conn: sqlite3.Connection,
    pack_sha256_list: list[str],
) -> dict[str, list[str]]:
    """Return deprecated/destroyed volume labels that hold packs from the list.

    These are packs that cannot be restored from active storage, but whose
    physical discs *may* still be retrievable if the operator has kept them.

    Returns:
        ``{volume_label: [sha256, ...]}`` — deprecated/destroyed volumes
        mapped to the packs they hold that are required for the restore.
        Only includes packs that have NO active-volume copy.
    """
    if not pack_sha256_list:
        return {}

    result: dict[str, list[str]] = {}

    for batch_start in range(0, len(pack_sha256_list), _SQLITE_BATCH):
        batch = pack_sha256_list[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)

        rows = conn.execute(
            f"""SELECT p.sha256, v.label AS vol_label, v.status AS vol_status
                FROM packs p
                JOIN volume_packs vp ON vp.pack_id = p.pack_id
                JOIN volumes v ON v.volume_id = vp.volume_id
                WHERE p.sha256 IN ({placeholders})
                  AND v.status IN ('DEPRECATED', 'DESTROYED')
                  AND NOT EXISTS (
                      SELECT 1 FROM volume_packs vp2
                      JOIN volumes v2 ON v2.volume_id = vp2.volume_id
                      WHERE vp2.pack_id = p.pack_id
                        AND v2.status NOT IN ('DEPRECATED', 'DESTROYED')
                  )
                ORDER BY v.label""",
            batch,
        ).fetchall()

        for row in rows:
            label = row["vol_label"]
            result.setdefault(label, []).append(row["sha256"])

    return result


def get_packs_only_on_volumes(
    conn: sqlite3.Connection,
    volume_ids: list[int],
) -> list[Pack]:
    """Return active (non-pruned) packs that exist on the given volumes.

    Used during consolidation to identify which packs from source volumes
    should be migrated to a new target volume.
    """
    if not volume_ids:
        return []

    # Process in batches to avoid SQLite variable limit.
    pack_map: dict[int, Pack] = {}
    for batch_start in range(0, len(volume_ids), _SQLITE_BATCH):
        batch = volume_ids[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT DISTINCT p.* FROM packs p
                JOIN volume_packs vp ON p.pack_id = vp.pack_id
                WHERE vp.volume_id IN ({placeholders})
                  AND p.is_pruned = 0
                ORDER BY p.sha256""",
            batch,
        ).fetchall()
        for r in rows:
            p = _row_to_pack(r)
            pack_map[p.pack_id] = p
    return sorted(pack_map.values(), key=lambda p: p.sha256)


def get_pruned_packs_on_volumes(
    conn: sqlite3.Connection,
    volume_ids: list[int],
) -> list[Pack]:
    """Return pruned packs present on the given volumes.

    Consolidation intentionally leaves these behind; surfacing them in
    the plan report makes the exclusion visible so a mis-pruned pack is
    caught (and unpruned) before its only discs are deprecated (BURN-09).
    """
    if not volume_ids:
        return []

    pack_map: dict[int, Pack] = {}
    for batch_start in range(0, len(volume_ids), _SQLITE_BATCH):
        batch = volume_ids[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT DISTINCT p.* FROM packs p
                JOIN volume_packs vp ON p.pack_id = vp.pack_id
                WHERE vp.volume_id IN ({placeholders})
                  AND p.is_pruned = 1
                ORDER BY p.sha256""",
            batch,
        ).fetchall()
        for r in rows:
            p = _row_to_pack(r)
            pack_map[p.pack_id] = p
    return sorted(pack_map.values(), key=lambda p: p.sha256)


def get_redundancy_report(
    conn: sqlite3.Connection,
    min_copies: int = 2,
) -> list[Pack]:
    """Return non-pruned packs with fewer than min_copies physical copies.

    Useful for ensuring every pack is stored on at least N discs.

    Replica truth (BURN-10/FMA-08): redundancy is counted in ACTIVE
    ``volume_copies`` rows, not volume rows — two ACTIVE copies of one
    volume are two discs, and a volume whose every physical copy is
    DEPRECATED/DESTROYED contributes nothing.  A volume with zero copy
    rows at all counts as one disc by status alone (legacy: volumes
    burned before copies were recorded, skip_burn fixtures, catalogs
    rebuilt from old discs).
    """
    rows = conn.execute(
        """SELECT p.*, COALESCE(SUM(
               CASE
                   WHEN v.volume_id IS NULL THEN 0
                   WHEN NOT EXISTS (SELECT 1 FROM volume_copies vc
                                    WHERE vc.volume_id = v.volume_id)
                       THEN 1
                   ELSE (SELECT COUNT(*) FROM volume_copies vc
                         WHERE vc.volume_id = v.volume_id
                           AND vc.status = 'ACTIVE')
               END), 0) as copy_count
           FROM packs p
           LEFT JOIN volume_packs vp ON p.pack_id = vp.pack_id
           LEFT JOIN volumes v ON vp.volume_id = v.volume_id
               AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
           WHERE p.is_pruned = 0
           GROUP BY p.pack_id
           HAVING copy_count < ?
           ORDER BY copy_count, p.sha256""",
        (min_copies,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_live_volumes_for_packs(
    conn: sqlite3.Connection,
    pack_ids: list[int],
) -> dict[int, list[Volume]]:
    """Map pack_id → volumes that count as live replicas of it (FMA-08).

    Live replica truth matches :func:`get_redundancy_report`: the volume
    is not DEPRECATED/DESTROYED and has >=1 ACTIVE ``volume_copies`` row
    (or zero copy rows at all — legacy).  Companion to the redundancy
    report: groups its under-replicated packs by the disc(s) actually
    holding them.
    """
    result: dict[int, list[Volume]] = {}
    for batch_start in range(0, len(pack_ids), _SQLITE_BATCH):
        batch = pack_ids[batch_start : batch_start + _SQLITE_BATCH]
        placeholders = ",".join("?" for _ in batch)
        rows = conn.execute(
            f"""SELECT vp.pack_id AS vp_pack_id, v.*
                FROM volume_packs vp
                JOIN volumes v ON v.volume_id = vp.volume_id
                WHERE vp.pack_id IN ({placeholders})
                  AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
                  AND (
                      EXISTS (SELECT 1 FROM volume_copies vc
                              WHERE vc.volume_id = v.volume_id
                                AND vc.status = 'ACTIVE')
                      OR NOT EXISTS (SELECT 1 FROM volume_copies vc2
                                     WHERE vc2.volume_id = v.volume_id)
                  )
                ORDER BY v.label""",
            batch,
        ).fetchall()
        for r in rows:
            result.setdefault(int(r["vp_pack_id"]), []).append(_row_to_volume(r))
    return result


def get_at_risk_packs_for_volume(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[Pack]:
    """Return packs whose ONLY live replica is the given volume (FMA-08).

    The blast radius of a disc: lose every physical copy of this volume
    and these packs are gone.  Replica truth matches
    ``check_deprecation_safe`` (``db/volumes.py``): another volume counts
    iff it is BURNED/VERIFIED and has >=1 ACTIVE ``volume_copies`` row
    (or zero copy rows at all — legacy).
    """
    rows = conn.execute(
        """SELECT p.*
           FROM volume_packs vp
           JOIN packs p ON p.pack_id = vp.pack_id
           WHERE vp.volume_id = ?
             AND p.is_pruned = 0
             AND NOT EXISTS (
                 SELECT 1 FROM volume_packs vp2
                 JOIN volumes v2 ON v2.volume_id = vp2.volume_id
                 WHERE vp2.pack_id = vp.pack_id
                   AND vp2.volume_id != ?
                   AND v2.status IN ('BURNED', 'VERIFIED')
                   AND (
                       EXISTS (SELECT 1 FROM volume_copies vc
                               WHERE vc.volume_id = v2.volume_id
                                 AND vc.status = 'ACTIVE')
                       OR NOT EXISTS (SELECT 1 FROM volume_copies vc2
                                      WHERE vc2.volume_id = v2.volume_id)
                   )
             )
           ORDER BY p.repo_id, p.sha256""",
        (volume_id, volume_id),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_packs_stranded_on_unburned_volumes(
    conn: sqlite3.Connection,
    older_than_hours: int = 24,
) -> list[Pack]:
    """Return packs whose ONLY volume claims are stale never-burned volumes.

    Since FMA-01 such packs DO reappear in the ``lcsas stage`` pool
    (STAGING/BURNING links are no longer "archived"), but the dead claims
    still clutter the catalog and mislead restore pick lists.  This
    surfaces those packs so the operator can run ``lcsas session abort``,
    ``lcsas stage --clean --force`` or ``lcsas catalog reconcile --fix``.

    The age cutoff (default 24 h) avoids flagging volumes that are simply
    mid-pipeline: a pack is returned only when every volume holding it is
    STAGING/BURNING *and* older than the cutoff.
    """
    modifier = f"-{int(older_than_hours)} hours"
    rows = conn.execute(
        """SELECT p.* FROM packs p
           WHERE p.is_pruned = 0
             AND EXISTS (
                 SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id
             )
             AND NOT EXISTS (
                 SELECT 1 FROM volume_packs vp
                 JOIN volumes v ON v.volume_id = vp.volume_id
                 WHERE vp.pack_id = p.pack_id
                   AND (v.status NOT IN ('STAGING', 'BURNING')
                        OR v.created_at > datetime('now', ?))
             )
           ORDER BY p.created_at""",
        (modifier,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_ghost_volumes(
    conn: sqlite3.Connection,
    older_than_hours: int = 24,
) -> list[Volume]:
    """Return stale never-burned volumes: STAGING/BURNING with zero copies.

    A ghost volume claims packs in the catalog but corresponds to no
    physical disc (no ``volume_copies`` row was ever recorded).  The age
    cutoff (default 24 h) avoids flagging sessions that are simply
    mid-pipeline.  ``lcsas catalog reconcile --fix`` deletes these.
    """
    modifier = f"-{int(older_than_hours)} hours"
    rows = conn.execute(
        """SELECT v.* FROM volumes v
           WHERE v.status IN ('STAGING', 'BURNING')
             AND NOT EXISTS (
                 SELECT 1 FROM volume_copies vc
                 WHERE vc.volume_id = v.volume_id
             )
             AND v.created_at <= datetime('now', ?)
           ORDER BY v.label""",
        (modifier,),
    ).fetchall()
    return [_row_to_volume(r) for r in rows]


def get_durable_volumes_without_active_copies(
    conn: sqlite3.Connection,
) -> list[Volume]:
    """Return volumes whose durable status disagrees with their copy records.

    A volume marked BURNED/VERIFIED/CONSOLIDATING should have at least one
    ACTIVE ``volume_copies`` row; one without is either a partially
    recorded burn or a copy-tracking bug (feeds the FMA-04/BURN-10
    volume-status/copies reconciliation).  Report-only — never auto-fixed.
    """
    rows = conn.execute(
        f"""SELECT v.* FROM volumes v
           WHERE v.status IN {_DURABLE_SQL}
             AND NOT EXISTS (
                 SELECT 1 FROM volume_copies vc
                 WHERE vc.volume_id = v.volume_id
                   AND vc.status = 'ACTIVE'
             )
           ORDER BY v.label"""
    ).fetchall()
    return [_row_to_volume(r) for r in rows]


def get_volume_pack_stats_by_repo(
    conn: sqlite3.Connection,
    volume_id: int,
) -> list[tuple[str, int, int]]:
    """Return ``(repo_id, pack_count, total_bytes)`` per repo for a volume."""
    rows = conn.execute(
        """SELECT p.repo_id, COUNT(*), COALESCE(SUM(p.size_bytes), 0)
           FROM packs p
           JOIN volume_packs vp ON vp.pack_id = p.pack_id
           WHERE vp.volume_id = ?
           GROUP BY p.repo_id
           ORDER BY p.repo_id""",
        (volume_id,),
    ).fetchall()
    return [(str(r[0]), int(r[1]), int(r[2])) for r in rows]


def get_archive_status_summary(
    conn: sqlite3.Connection,
) -> dict[str, int]:
    """Return a summary of archive status by pack bucket.

    Buckets partition the non-pruned packs (FMA-01 semantics):

    - ``archived``   — linked to at least one durable (BURNED/VERIFIED/
      CONSOLIDATING) volume: the pack is on a disc that exists.
    - ``staged``     — linked only to STAGING/BURNING volumes: claimed by
      a burn-in-progress (or a ghost volume), NOT yet on any disc.
    - ``unarchived`` — no volume links at all.
    """
    row = conn.execute(
        f"""SELECT
               COUNT(*) as total,
               SUM(CASE WHEN is_pruned = 1 THEN 1 ELSE 0 END) as pruned,
               SUM(CASE WHEN is_pruned = 0 AND EXISTS (
                   SELECT 1 FROM volume_packs vp
                   JOIN volumes v ON v.volume_id = vp.volume_id
                   WHERE vp.pack_id = packs.pack_id
                     AND v.status IN {_DURABLE_SQL}
               ) THEN 1 ELSE 0 END) as archived,
               SUM(CASE WHEN is_pruned = 0 AND EXISTS (
                   SELECT 1 FROM volume_packs vp WHERE vp.pack_id = packs.pack_id
               ) AND NOT EXISTS (
                   SELECT 1 FROM volume_packs vp
                   JOIN volumes v ON v.volume_id = vp.volume_id
                   WHERE vp.pack_id = packs.pack_id
                     AND v.status IN {_DURABLE_SQL}
               ) THEN 1 ELSE 0 END) as staged,
               SUM(CASE WHEN is_pruned = 0 AND NOT EXISTS (
                   SELECT 1 FROM volume_packs vp WHERE vp.pack_id = packs.pack_id
               ) THEN 1 ELSE 0 END) as unarchived
           FROM packs"""
    ).fetchone()
    if row is None:
        raise RuntimeError("get_archive_status_summary: aggregate query returned no row")
    return {
        "total": int(row[0]),
        "pruned": int(row[1] or 0),
        "archived": int(row[2] or 0),
        "staged": int(row[3] or 0),
        "unarchived": int(row[4] or 0),
    }


def get_unarchived_or_missing_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> list[Pack]:
    """Return packs that either have no volume at all OR have no ACTIVE copy
    at the specified location. This is the full set needed to bring a
    location completely up to date.
    """
    rows = conn.execute(
        """SELECT p.* FROM packs p
           WHERE p.is_pruned = 0
             AND p.pack_id NOT IN (
                 SELECT DISTINCT vp.pack_id
                 FROM volume_packs vp
                 JOIN volume_copies vc ON vc.volume_id = vp.volume_id
                 WHERE vc.location = ?
                   AND vc.status = 'ACTIVE'
             )
           ORDER BY p.created_at""",
        (location,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


# ---------------------------------------------------------------------------
# Location-aware queries
# ---------------------------------------------------------------------------


def get_packs_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> set[int]:
    """Return set of pack IDs that have at least one ACTIVE copy at location."""
    rows = conn.execute(
        """SELECT DISTINCT vp.pack_id
           FROM volume_packs vp
           JOIN volume_copies vc ON vc.volume_id = vp.volume_id
           WHERE vc.location = ?
             AND vc.status = 'ACTIVE'""",
        (location,),
    ).fetchall()
    return {row["pack_id"] for row in rows}


def get_packs_missing_at_location(
    conn: sqlite3.Connection,
    location: str,
) -> list[Pack]:
    """Return packs that have been archived but have no ACTIVE copy at location.

    This identifies packs that need to be staged and burned for a location
    to bring it up to date.
    """
    rows = conn.execute(
        """SELECT p.* FROM packs p
           WHERE p.is_pruned = 0
             AND p.pack_id IN (SELECT pack_id FROM volume_packs)
             AND p.pack_id NOT IN (
                 SELECT DISTINCT vp.pack_id
                 FROM volume_packs vp
                 JOIN volume_copies vc ON vc.volume_id = vp.volume_id
                 WHERE vc.location = ?
                   AND vc.status = 'ACTIVE'
             )
           ORDER BY p.created_at""",
        (location,),
    ).fetchall()
    return [_row_to_pack(r) for r in rows]


def get_location_summary(
    conn: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Summary of each location: volume count, pack count, packs behind."""
    total_archived = conn.execute(
        """SELECT COUNT(DISTINCT pack_id) FROM volume_packs"""
    ).fetchone()[0]

    rows = conn.execute(
        """SELECT
               vc.location,
               COUNT(DISTINCT vc.volume_id) AS volume_count,
               COUNT(DISTINCT vp.pack_id) AS pack_count
           FROM volume_copies vc
           JOIN volume_packs vp ON vp.volume_id = vc.volume_id
           JOIN volumes v ON v.volume_id = vc.volume_id
           WHERE vc.status = 'ACTIVE' AND v.status NOT IN ('DEPRECATED', 'DESTROYED')
           GROUP BY vc.location
           ORDER BY vc.location"""
    ).fetchall()
    return [
        {
            "location": r["location"],
            "volumes": r["volume_count"],
            "packs": r["pack_count"],
            "missing": total_archived - r["pack_count"],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Snapshot JSON helpers  (requires SQLite 3.9+ for json_each)
# ---------------------------------------------------------------------------


def get_snapshots_by_path(
    conn: sqlite3.Connection,
    path_pattern: str,
    repo_id: str | None = None,
) -> list[Snapshot]:
    """Return snapshots containing a path matching *path_pattern*.

    Uses SQLite ``json_each()`` to search the JSON array stored in
    ``snapshots.paths``.  The *path_pattern* supports SQL LIKE wildcards
    (``%`` and ``_``).
    """
    if repo_id:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE s.repo_id = ?
                 AND EXISTS (
                     SELECT 1 FROM json_each(s.paths)
                     WHERE value LIKE ?
                 )
               ORDER BY s.timestamp DESC""",
            (repo_id, path_pattern),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE EXISTS (
                   SELECT 1 FROM json_each(s.paths)
                   WHERE value LIKE ?
               )
               ORDER BY s.timestamp DESC""",
            (path_pattern,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]


def get_snapshots_by_tag(
    conn: sqlite3.Connection,
    tag: str,
    repo_id: str | None = None,
) -> list[Snapshot]:
    """Return snapshots that contain the exact *tag*.

    Uses SQLite ``json_each()`` on the ``snapshots.tags`` JSON array.
    """
    if repo_id:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE s.repo_id = ?
                 AND EXISTS (
                     SELECT 1 FROM json_each(s.tags)
                     WHERE value = ?
                 )
               ORDER BY s.timestamp DESC""",
            (repo_id, tag),
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT s.* FROM snapshots s
               WHERE EXISTS (
                   SELECT 1 FROM json_each(s.tags)
                   WHERE value = ?
               )
               ORDER BY s.timestamp DESC""",
            (tag,),
        ).fetchall()
    return [_row_to_snapshot(r) for r in rows]
