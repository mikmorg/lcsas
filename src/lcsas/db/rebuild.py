"""Catalog rebuild — merge disc catalogs into a new master catalog."""

from __future__ import annotations

import json
import logging
import sqlite3
import urllib.parse
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from lcsas.db.disc_catalog import (
    DiscCatalogError,
    disc_catalog_uri,
    open_disc_catalog,
)
from lcsas.db.lifecycle import VolumeLifecycle
from lcsas.db.repos import REPO_STATUSES
from lcsas.db.schema import CURRENT_SCHEMA_VERSION
from lcsas.db.snapshots import DISC_BLANKED_SNAPSHOT_COLUMNS
from lcsas.db.volume_events import VALID_EVENT_TYPES

_logger = logging.getLogger(__name__)

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


# The values this LCSAS knows in each column that has a fixed set: the
# CHECK constraints in schema.py, and repositories.status, which is
# checked in Python (#963).  A disc catalog holding any other value was
# written by a newer LCSAS (or is damaged): its disc is refused, never
# merged with the row silently dropped.
_KNOWN_VALUES: tuple[tuple[str, str, frozenset[str]], ...] = (
    ("volumes", "status", frozenset(_STATUS_RANK)),
    ("volume_copies", "status", frozenset({"ACTIVE", "DEPRECATED", "DESTROYED"})),
    ("burn_sessions", "status",
     frozenset({"STAGED", "PARTIAL", "COMPLETE", "CLEANED"})),
    ("volume_events", "event_type", VALID_EVENT_TYPES),
    ("repositories", "status", frozenset(REPO_STATUSES)),
)


# julianday() of 1970-01-01T00:00:00Z.
_UNIX_EPOCH_JULIAN_DAY = 2440587.5

# Freshness of a catalog: (newest timestamp found in its rows, file mtime).
# Tuples compare element by element, so the rows decide and the mtime only
# breaks a tie (#686).
Freshness = tuple[float, float]

# Timestamp columns that show how recent a catalog is.  Each is read with
# its own MAX(julianday()), because the columns do not share one text
# format (CURRENT_TIMESTAMP vs isoformat with an offset, #730).
_FRESHNESS_COLUMNS = (
    ("volumes", "created_at"),
    ("volumes", "closed_at"),
    ("volumes", "verified_at"),
    ("volume_copies", "burn_date"),
    ("volume_copies", "last_verified_at"),
    ("volume_events", "event_date"),
    ("burn_sessions", "created_at"),
    ("key_escrow", "split_at"),
)


def _row_freshness(conn: sqlite3.Connection, schema: str = "main") -> float:
    """Newest timestamp in the rows of a catalog (``-inf`` when none).

    A catalog older than v10 lacks some of the tables; those are skipped.
    """
    tables = {
        r[0] for r in conn.execute(
            f"SELECT name FROM {schema}.sqlite_master WHERE type = 'table'"
        )
    }
    newest = float("-inf")
    for table, column in _FRESHNESS_COLUMNS:
        if table not in tables:
            continue
        # #730: the newest instant, not the greatest text: a column can
        # mix CURRENT_TIMESTAMP text, isoformat and other offsets.
        try:
            row = conn.execute(
                f"SELECT MAX(julianday({column})) FROM {schema}.{table}"
            ).fetchone()
        except sqlite3.Error:
            continue  # column missing in an old catalog
        if row is None or row[0] is None:
            continue
        ts = (float(row[0]) - _UNIX_EPOCH_JULIAN_DAY) * 86400.0
        if ts > newest:
            newest = ts
    return newest


def _source_freshness(catalog_db: Path) -> Freshness:
    """Freshness of a disc catalog (bigger = newer).

    Ranked by the newest timestamp in the catalog's rows first; the file
    mtime (preserved by Rock Ridge in the ISO image) only breaks a tie.
    A catalog copied without keeping its mtime therefore cannot outrank
    a catalog whose rows are newer (FMA-06, #686).
    """
    mtime = catalog_db.stat().st_mtime
    try:
        # Read-only and immutable (#963): never replays or removes a
        # -wal or -journal file beside the source.
        conn = sqlite3.connect(disc_catalog_uri(catalog_db), uri=True)
        try:
            rows = _row_freshness(conn)
        finally:
            conn.close()
    except sqlite3.Error:
        # Unreadable/corrupt source — the merge itself will report it.
        rows = float("-inf")
    return (rows, mtime)


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
    events_merged: int = 0
    sessions_merged: int = 0
    session_volumes_merged: int = 0
    splits_merged: int = 0
    # #963: the date of the newest catalog read (its newest row) and the
    # disc it came from.  The rebuilt catalog cannot know what happened
    # after that date.
    as_of: str = ""
    newest_disc: str = ""
    # Labels of volumes this rebuild moved from STAGING to BURNED because
    # their own disc was one of the inputs (#678).  No disc record says
    # where those discs are kept; ``lcsas verify <label> --disc
    # --location <where>`` records it.
    needs_verify: list[str] = field(default_factory=list)
    # Labels of volumes this rebuild brought in as STAGING/BURNING with no
    # disc record and no burn evidence (#758): no disc that was read shows
    # whether they were burned.  Each got a NOTE (REBUILD_UNKNOWN_NOTE_
    # PREFIX), so session abort, stage --clean and reconcile --fix refuse
    # to delete them unless the owner confirms they were never burned.
    burn_unknown: list[str] = field(default_factory=list)
    # #957: the highest label number any disc read here knows (the label
    # mark), and the label of a volume with that number if one was read.
    # ``stage`` numbers new volumes after it.
    label_mark: int = 0
    label_mark_label: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _fill_blanked_snapshots(target: sqlite3.Connection, alias: str) -> None:
    """Fill blanked snapshot rows from the attached disc catalog (#733).

    A disc catalog staged since #733 has no source folders, tags or
    description (``DISC_BLANKED_SNAPSHOT_COLUMNS``).  Discs merge
    newest-first, so a blanked row usually lands first; an older disc
    that still has the values fills it here.  Only a row whose every
    such column is blank is filled, and only from a source row that has
    at least one value.  The next ``lcsas scan`` writes paths and tags
    from rustic anyway.
    """
    src_cols = {
        str(r[1]) for r in target.execute(
            f"PRAGMA {alias}.table_info(snapshots)"
        ).fetchall()
    }
    cols = [c for c in DISC_BLANKED_SNAPSHOT_COLUMNS if c in src_cols]
    if not cols:
        return
    blank_values = [DISC_BLANKED_SNAPSHOT_COLUMNS[c] for c in cols]
    target_blank = " AND ".join(f"snapshots.{c} = ?" for c in cols)
    source_set = " OR ".join(f"s.{c} != ?" for c in cols)
    assignments = ", ".join(
        f"{c} = (SELECT s.{c} FROM {alias}.snapshots AS s "
        f"WHERE s.snapshot_id = snapshots.snapshot_id)"
        for c in cols
    )
    target.execute(
        f"""
        UPDATE snapshots SET {assignments}
        WHERE {target_blank}
          AND EXISTS (
            SELECT 1 FROM {alias}.snapshots AS s
            WHERE s.snapshot_id = snapshots.snapshot_id
              AND ({source_set})
          )
        """,
        [*blank_values, *blank_values],
    )


def _merge_one_disc(
    target: sqlite3.Connection,
    source_db: Path,
    *,
    source_freshness: Freshness = (0.0, 0.0),
    status_freshness: dict[str, Freshness] | None = None,
    warnings: list[str] | None = None,
    escrow_freshness: dict[str, Freshness] | None = None,
) -> dict[str, int]:
    """Attach *source_db* and merge its data into *target*.

    Inserts with ON CONFLICT (natural key) DO NOTHING, so records are only added
    when they do not already exist.  Volume-status conflicts are resolved
    by recency (FMA-06): the freshest catalog that mentions a volume owns
    its status — including downgrades to DEPRECATED/DESTROYED.

    *status_freshness* maps volume uuid → freshness of the catalog that
    last set its status; it is shared across all merges of one rebuild
    run.  When the source is staler than that record, its status is
    ignored — and if the stale status ranks *more alive* (the
    resurrection hazard) a warning is appended to *warnings*.

    *escrow_freshness* does the same for recorded password splits: it maps
    a repository key (``key_escrow.repo_id``) to the freshness of the
    catalog that decided its split record (#686).

    Returns a dict mapping table name → rows inserted (plus the special
    key ``is_pruned_conflicts``: packs where a staler source disagreed
    with the kept ``is_pruned`` flag).
    """
    counts: dict[str, int] = {}
    if status_freshness is None:
        status_freshness = {}
    if warnings is None:
        warnings = []
    if escrow_freshness is None:
        escrow_freshness = {}

    # #820: a disc whose merge fails must leave no trace: its rows are
    # rolled back, and the freshness maps and warnings it touched are put
    # back as they were, so the next disc merges as if it had never been
    # read.
    saved_status = dict(status_freshness)
    saved_escrow = dict(escrow_freshness)
    saved_warning_count = len(warnings)

    # Use a safe alias to avoid conflicts with other attached catalogs.
    alias = "src"
    # Commit anything the caller left open: a failed merge rolls back to
    # here, and must not take the caller's work with it.
    target.commit()
    # #963: attached read-only and immutable.  A plain ATTACH opened the
    # source read-write, and SQLite then replayed a hot -journal or
    # checkpointed a -wal beside it: the rebuild changed a source.  The
    # target connection takes URI file names (_open_output).
    target.execute(
        f"ATTACH DATABASE ? AS {alias}", (disc_catalog_uri(source_db),),
    )
    try:
        # A connection that does not read URI names would attach (and
        # create) a file literally named "file:...": merge nothing then.
        attached = {
            str(r[1]): str(r[2])
            for r in target.execute("PRAGMA database_list").fetchall()
        }
        if Path(attached.get(alias) or "/").resolve() != source_db.resolve():
            raise DiscCatalogError(
                f"could not attach {source_db} read-only: this connection "
                "does not read SQLite URI file names"
            )
        # 1. repositories — keyed on repo_id.
        #
        # `status` (schema v10) must travel with the row.  Discs are merged
        # freshest-first and a row already there is kept, so the freshest
        # catalog that mentions a repository owns its status — the same rule
        # volumes follow below.  Dropping the column instead would silently
        # resurrect every retired repository as 'active' (the table default),
        # and the next `lcsas meta build` would then hard-fail on a repository
        # the owner had already retired (#437) — right after the catalog loss
        # that made a rebuild necessary.  The disc is the record; preserve
        # what it recorded.
        #
        # A v≤9 disc catalog has no such column, so probe before selecting
        # it: those repositories pre-date retirement and are therefore active.
        src_repo_cols = {
            r[1] for r in target.execute(
                f"PRAGMA {alias}.table_info(repositories)"
            ).fetchall()
        }
        status_expr = "status" if "status" in src_repo_cols else "'active'"
        cur = target.execute(
            f"""
            INSERT INTO repositories (repo_id, name, mirror_path,
                encryption_key_id, created_at, status)
            SELECT repo_id, name, mirror_path, encryption_key_id, created_at,
                {status_expr}
            FROM {alias}.repositories
            WHERE true
            ON CONFLICT (repo_id) DO NOTHING
            """
        )
        counts["repositories"] = cur.rowcount

        # 2. locations — keyed on name
        cur = target.execute(
            f"""
            INSERT INTO locations (name, description, created_at)
            SELECT name, description, created_at
            FROM {alias}.locations
            WHERE true
            ON CONFLICT (name) DO NOTHING
            """
        )
        counts["locations"] = cur.rowcount

        # 3. packs — keyed on sha256.  rebuild_catalog() merges discs
        #    newest-first, so the row already in the target was written by
        #    a fresher catalog and ON CONFLICT DO NOTHING keeps the freshest
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
            INSERT INTO packs (sha256, size_bytes, repo_id,
                is_pruned, created_at)
            SELECT sha256, size_bytes, repo_id, is_pruned, created_at
            FROM {alias}.packs
            WHERE true
            ON CONFLICT (sha256) DO NOTHING
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
            INSERT INTO volumes
                (label, uuid, media_type, capacity_bytes, used_bytes,
                 location, status, created_at, closed_at, verified_at)
            SELECT label, uuid, media_type, capacity_bytes, used_bytes,
                   location, status, created_at, closed_at, verified_at
            FROM {alias}.volumes
            WHERE true
            ON CONFLICT (uuid) DO NOTHING
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
                    VolumeLifecycle(target).disc_catalog_status_adopted(
                        volume_id, src_status,
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
                    f"keeping {tgt_status}. If you physically hold a disc of "
                    f"this volume, verify it with 'lcsas verify {label} --disc'."
                )

        # 5. snapshots — keyed on snapshot_id
        cur = target.execute(
            f"""
            INSERT INTO snapshots
                (snapshot_id, repo_id, hostname, timestamp,
                 paths, tags, description)
            SELECT snapshot_id, repo_id, hostname, timestamp,
                   paths, tags, description
            FROM {alias}.snapshots
            WHERE true
            ON CONFLICT (snapshot_id) DO NOTHING
            """
        )
        counts["snapshots"] = cur.rowcount
        _fill_blanked_snapshots(target, alias)

        # 6. volume_packs — keyed on (volume_id, pack_id).
        #    We must translate IDs from the source catalog since auto-increment
        #    IDs differ between catalogs.  Join on natural keys (uuid, sha256).
        cur = target.execute(
            f"""
            INSERT INTO volume_packs (volume_id, pack_id)
            SELECT v.volume_id, p.pack_id
            FROM {alias}.volume_packs svp
            JOIN {alias}.volumes sv ON sv.volume_id = svp.volume_id
            JOIN {alias}.packs  sp ON sp.pack_id   = svp.pack_id
            JOIN volumes v ON v.uuid   = sv.uuid
            JOIN packs   p ON p.sha256 = sp.sha256
            WHERE true
            ON CONFLICT (volume_id, pack_id) DO NOTHING
            """
        )
        counts["volume_packs"] = cur.rowcount

        # 7. volume_copies — keyed on (volume_id, location).
        #    iso_size_bytes arrived in schema v8 (FMA-03); disc catalogs
        #    burned before that lack the column, so select it only when
        #    the source actually has it (disc catalogs are frozen
        #    forever — they can never be migrated in place).
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
            INSERT INTO volume_copies
                (volume_id, location, status, burn_date, notes, iso_sha256,
                 iso_size_bytes, last_verified_at, media_serial)
            SELECT v.volume_id, svc.location, svc.status, svc.burn_date,
                   svc.notes, svc.iso_sha256, {size_expr},
                   svc.last_verified_at, svc.media_serial
            FROM {alias}.volume_copies svc
            JOIN {alias}.volumes sv ON sv.volume_id = svc.volume_id
            JOIN volumes v ON v.uuid = sv.uuid
            WHERE true
            ON CONFLICT (volume_id, location) DO NOTHING
            """
        )
        counts["volume_copies"] = cur.rowcount

        # 8. Provenance/audit tables (FMA-10).  These arrived after the
        #    earliest disc catalogs, so a disc burned before them
        #    simply lacks the table; probe sqlite_master and skip the
        #    merge for that disc rather than failing the whole rebuild.
        src_tables = {
            r[0] for r in target.execute(
                f"SELECT name FROM {alias}.sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        # 8a. volume_events — keyed on (volume uuid, event_type, event_date).
        #     The event_id autoincrement differs across catalogs, so translate
        #     volume_id via uuid and dedupe on the
        #     natural triple (enforced by the unique index below at write
        #     time; here we guard with NOT EXISTS since the live schema's
        #     PK is the surrogate event_id).
        if "volume_events" in src_tables:
            cur = target.execute(
                f"""
                INSERT INTO volume_events
                    (volume_id, event_type, event_date, location, detail)
                SELECT v.volume_id, se.event_type, se.event_date,
                       se.location, se.detail
                FROM {alias}.volume_events se
                JOIN {alias}.volumes sv ON sv.volume_id = se.volume_id
                JOIN volumes v ON v.uuid = sv.uuid
                WHERE NOT EXISTS (
                    SELECT 1 FROM volume_events te
                    WHERE te.volume_id  = v.volume_id
                      AND te.event_type = se.event_type
                      AND te.event_date = se.event_date
                      AND te.detail     = se.detail
                )
                """
            )
            counts["volume_events"] = cur.rowcount
        else:
            counts["volume_events"] = 0

        # 8b. burn_sessions — keyed on session_id (a global UUID-ish text
        #     key, identical across catalogs).
        if "burn_sessions" in src_tables:
            cur = target.execute(
                f"""
                INSERT INTO burn_sessions
                    (session_id, created_at, media_type, status, staging_dir)
                SELECT session_id, created_at, media_type, status, staging_dir
                FROM {alias}.burn_sessions
                WHERE true
                ON CONFLICT (session_id) DO NOTHING
                """
            )
            counts["burn_sessions"] = cur.rowcount
        else:
            counts["burn_sessions"] = 0

        # 8c. session_volumes — keyed on (session_id, volume_id) with the
        #     volume_id translated via uuid.  iso_size_bytes arrived in
        #     schema v7; select it only when the source column exists.
        if "session_volumes" in src_tables:
            src_sv_cols = {
                r[1] for r in target.execute(
                    f"PRAGMA {alias}.table_info(session_volumes)"
                ).fetchall()
            }
            sv_size_expr = (
                "ssv.iso_size_bytes" if "iso_size_bytes" in src_sv_cols
                else "NULL"
            )
            cur = target.execute(
                f"""
                INSERT INTO session_volumes
                    (session_id, volume_id, iso_path, iso_sha256,
                     iso_size_bytes)
                SELECT ssv.session_id, v.volume_id, ssv.iso_path,
                       ssv.iso_sha256, {sv_size_expr}
                FROM {alias}.session_volumes ssv
                JOIN {alias}.volumes sv ON sv.volume_id = ssv.volume_id
                JOIN volumes v ON v.uuid = sv.uuid
                WHERE true
                ON CONFLICT (session_id, volume_id) DO NOTHING
                """
            )
            counts["session_volumes"] = cur.rowcount
        else:
            counts["session_volumes"] = 0

        # 9. key_escrow (KEY-08, #573) — the recorded password splits, keyed
        #    on repo_id.  Every disc catalog carries them; dropping them made
        #    the stage preflight refuse a rebuilt catalog ("the catalog
        #    records no split") until the owner split the password again,
        #    which supersedes the share cards already handed out.
        #
        #    The freshest catalog that knows a repository decides its split
        #    record, the same rule as volume status (#686): a split that was
        #    recorded on an older disc and cleared before a newer disc was
        #    staged must stay cleared, so a newer catalog with NO row for the
        #    repository removes the row an older one would bring back.  A
        #    catalog "knows" a repository it lists (by repo_id or by name:
        #    splits recorded before #571 are keyed by name) or holds a split
        #    for.  A disc burned before the table existed decides nothing.
        inserted = 0
        if "key_escrow" in src_tables:
            known: set[str] = set()
            for repo_id, name in target.execute(
                f"SELECT repo_id, name FROM {alias}.repositories"
            ).fetchall():
                known.update((repo_id, name))
            known.update(
                r[0] for r in target.execute(
                    f"SELECT repo_id FROM {alias}.key_escrow"
                ).fetchall()
            )
            for key in sorted(known):
                last = escrow_freshness.get(key)
                if last is not None and not source_freshness > last:
                    continue
                target.execute(
                    "DELETE FROM key_escrow WHERE repo_id = ?", (key,)
                )
                cur = target.execute(
                    f"""
                    INSERT INTO key_escrow
                        (repo_id, threshold, shares, slip39_id, split_at)
                    SELECT repo_id, threshold, shares, slip39_id, split_at
                    FROM {alias}.key_escrow WHERE repo_id = ?
                    """,
                    (key,),
                )
                inserted += max(cur.rowcount, 0)
                escrow_freshness[key] = source_freshness
        counts["key_escrow"] = inserted

        target.commit()

    except BaseException:
        # #820: undo this disc's partial merge.  An open transaction also
        # keeps the attached file locked, so DETACH below would fail and
        # every later disc would find "src" still attached.
        target.rollback()
        status_freshness.clear()
        status_freshness.update(saved_status)
        escrow_freshness.clear()
        escrow_freshness.update(saved_escrow)
        del warnings[saved_warning_count:]
        raise
    finally:
        try:
            target.execute(f"DETACH DATABASE {alias}")
        except sqlite3.Error as exc:  # pragma: no cover - defensive
            _logger.error("Could not detach %s: %s", source_db, exc)

    return counts


def _own_volume_uuid(disc_path: Path) -> str | None:
    """The uuid in a disc's ``volume_info.json`` (None if absent/unreadable)."""
    try:
        info = json.loads(
            (disc_path / "volume_info.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    uuid_ = info.get("uuid") if isinstance(info, dict) else None
    return uuid_ if isinstance(uuid_, str) and uuid_ else None


def _disc_label(disc_path: Path) -> str | None:
    """The label in a disc's ``volume_info.json`` (None if absent/unreadable)."""
    try:
        info = json.loads(
            (disc_path / "volume_info.json").read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    label = info.get("label") if isinstance(info, dict) else None
    return label if isinstance(label, str) and label else None


def _disc_name(disc_path: Path) -> str:
    """How a message names a disc: its label (from ``volume_info.json``)
    and the folder it was read from, or the folder alone."""
    label = _disc_label(disc_path)
    if label is not None:
        return f"disc {label} ({disc_path})"
    return f"disc at {disc_path}"


@dataclass
class _Source:
    """One disc catalog as read before anything is merged (#963)."""

    disc_path: Path
    catalog_db: Path
    freshness: Freshness
    version: int | None = None
    # label -> uuid of every volume the catalog lists.
    labels: dict[str, str] = field(default_factory=dict)
    # Why the disc is not merged; empty when it is.
    refusal: str = ""

    @property
    def name(self) -> str:
        return _disc_name(self.disc_path)

    @property
    def newer(self) -> bool:
        return self.version is not None and self.version > CURRENT_SCHEMA_VERSION

    def merge_failure(self, exc: BaseException) -> str:
        """The error line for a merge of this disc that failed."""
        if self.newer:
            return (
                f"{self.name}: written by a newer LCSAS (schema "
                f"v{self.version}): rebuild with that version or newer "
                f"({exc}). Nothing from it was merged."
            )
        return (
            f"{self.name}: its catalog is damaged or unreadable ({exc}). "
            f"Nothing from it was merged."
        )


def _unknown_values(conn: sqlite3.Connection) -> list[tuple[str, str, object]]:
    """``(table, column, value)`` for each value in *conn* that this LCSAS
    does not know (``_KNOWN_VALUES``).  A table or column the catalog
    lacks (an older schema) has none."""
    tables = {
        str(r[0]) for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    unknown: list[tuple[str, str, object]] = []
    for table, column, known in _KNOWN_VALUES:
        if table not in tables:
            continue
        cols = {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            continue
        for (value,) in conn.execute(
            f"SELECT DISTINCT {column} FROM {table} ORDER BY 1"
        ):
            if value not in known:
                unknown.append((table, column, value))
    return unknown


def _read_source(disc_path: Path, catalog_db: Path) -> _Source:
    """Read one disc catalog read-only, before any merge (#963): its
    freshness, schema version and labels, and whether it can be merged.

    A catalog that cannot be read, or that holds a value this LCSAS does
    not know, gets a ``refusal``.  The wording says "newer" for a
    catalog of a newer schema, never "damaged".
    """
    source = _Source(disc_path, catalog_db,
                     (float("-inf"), catalog_db.stat().st_mtime))
    try:
        conn = open_disc_catalog(catalog_db, allow_newer=True)
    except DiscCatalogError as exc:
        reason = exc.__cause__ if exc.__cause__ is not None else exc
        source.refusal = (
            f"{source.name}: its catalog is damaged or unreadable "
            f"({reason}). Nothing from it was merged."
        )
        return source
    try:
        row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        source.version = int(row[0])
        source.freshness = (_row_freshness(conn), source.freshness[1])
        unknown = _unknown_values(conn)
        source.labels = {
            str(label): str(uuid_) for label, uuid_ in conn.execute(
                "SELECT label, uuid FROM volumes"
            )
        }
    except sqlite3.Error as exc:
        source.refusal = source.merge_failure(exc)
        return source
    finally:
        conn.close()
    if unknown:
        values = "; ".join(f"{t}.{c} holds {v!r}" for t, c, v in unknown)
        if source.newer:
            source.refusal = (
                f"{source.name}: written by a newer LCSAS (schema "
                f"v{source.version}): rebuild with that version or newer. "
                f"Its catalog holds values this LCSAS does not know "
                f"({values}). Nothing from it was merged."
            )
        else:
            source.refusal = (
                f"{source.name}: its catalog holds values this LCSAS does "
                f"not know ({values}), though it says schema "
                f"v{source.version}: it may be damaged. Nothing from it was "
                f"merged."
            )
    return source


def _label_clashes(
    sources: list[_Source], existing: dict[str, tuple[str, str]],
) -> list[str]:
    """One message per label that names two different volumes (#957, #963).

    *existing* maps a label to ``(uuid, where)`` for volumes the output
    catalog already holds.  The catalog holds one volume per label; the
    merge used to keep the first and drop the other without a word.
    """
    seen: dict[str, dict[str, list[str]]] = {}
    for label, (uuid_, where) in existing.items():
        seen.setdefault(label, {}).setdefault(uuid_, []).append(where)
    for source in sources:
        for label, uuid_ in source.labels.items():
            seen.setdefault(label, {}).setdefault(uuid_, []).append(source.name)
    messages = []
    for label, by_uuid in sorted(seen.items()):
        if len(by_uuid) < 2:
            continue
        found = "; ".join(
            f"uuid {uuid_} in {', '.join(names)}"
            for uuid_, names in sorted(by_uuid.items())
        )
        messages.append(
            f"Label {label} names {len(by_uuid)} different volumes: {found}. "
            f"Two discs carry one label: either a catalog rebuilt without "
            f"its newest disc numbered a later volume the same, or a disc of "
            f"another archive is among these discs (check its backup sets). "
            f"A catalog holds one volume per label, so nothing was rebuilt. "
            f"Rebuild again without the discs that list one of them (a disc "
            f"of another archive, else the older disc of that label, which "
            f"later discs do not list) and keep that disc: the rebuilt "
            f"catalog will not know it."
        )
    return messages


def _open_output(output_db: Path) -> sqlite3.Connection:
    """The output catalog, created or brought to the current schema, on a
    connection that reads URI file names, so a source can be attached
    read-only and immutable (#963) whatever SQLite's build default."""
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import ensure_schema

    # Source disc catalogs are attached read-only and are never migrated:
    # only the output catalog goes through ensure_schema.
    conn = get_connection(output_db)
    try:
        ensure_schema(conn)
    finally:
        conn.close()
    uri = "file:" + urllib.parse.quote(str(Path(output_db).resolve()))
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def _promote_read_discs(
    conn: sqlite3.Connection,
    own_uuids: set[str],
    result: RebuildResult,
) -> None:
    """Mark volumes whose own disc was read as BURNED (#678).

    A disc catalog is copied onto the disc when the volume is staged, so
    the newest burn session's volumes are STAGING in every input catalog,
    with no disc record.  A disc that was just read proves its volume
    was burned, so such a volume becomes BURNED (its packs no longer
    return to the stage pool).  Where the disc is kept and whether it
    was verified are not on any disc: the volume is listed in
    ``result.needs_verify`` for ``lcsas verify --disc --location``.

    A volume with a VERIFY_FAIL event is left as it is: a newer catalog
    records that a burn of it failed its check, so the disc that was
    read may be that failed disc.
    """
    for uuid_ in sorted(own_uuids):
        row = conn.execute(
            "SELECT volume_id, label, status FROM volumes WHERE uuid = ?",
            (uuid_,),
        ).fetchone()
        if row is None or row[2] not in ("STAGING", "BURNING"):
            continue
        volume_id, label = row[0], row[1]
        failed = conn.execute(
            "SELECT 1 FROM volume_events WHERE volume_id = ? "
            "AND event_type = 'VERIFY_FAIL' LIMIT 1",
            (volume_id,),
        ).fetchone()
        if failed is not None:
            result.warnings.append(
                f"volume {label}: its disc was read, but a catalog records "
                f"that a burn of it failed verification, so it stays "
                f"{row[2]}. Check the disc with 'lcsas verify {label} "
                f"--disc --location <where it is kept>'."
            )
            continue
        VolumeLifecycle(conn).own_disc_read_in_rebuild(volume_id)
        result.needs_verify.append(label)
    conn.commit()
    result.needs_verify.sort()


def _mark_burn_unknown(
    conn: sqlite3.Connection,
    known_uuids: set[str],
    result: RebuildResult,
) -> None:
    """Mark the volumes whose burn state no disc that was read shows (#758).

    A disc catalog lists every volume staged before its disc as it was
    then, so a volume of a newer burn session whose own disc was not read
    comes back STAGING with no disc record and no events, which is how a
    never-burned volume looks.  Each volume this rebuild brought in (not
    in the output catalog before it) that is STAGING/BURNING with no disc
    record and no burn evidence gets a NOTE starting with
    ``REBUILD_UNKNOWN_NOTE_PREFIX`` and is listed in
    ``result.burn_unknown``.  A volume that already has the note (a disc
    staged from a rebuilt catalog carries it) is listed, not noted again;
    one the output catalog already had is neither.

    The note is dated with the volume's ``created_at``, never "now": the
    catalog's newest timestamp is its freshness (``_row_freshness``), and
    a note newer than every disc would stop a later rebuild into this
    catalog from taking a fresher disc's view of any volume.
    """
    from lcsas.db.volume_events import REBUILD_UNKNOWN_NOTE_PREFIX, add_event

    lifecycle = VolumeLifecycle(conn)
    rows = conn.execute(
        """SELECT v.volume_id, v.label, v.uuid, v.created_at FROM volumes v
           WHERE v.status IN ('STAGING', 'BURNING')
             AND NOT EXISTS (SELECT 1 FROM volume_copies vc
                             WHERE vc.volume_id = v.volume_id)
           ORDER BY v.label"""
    ).fetchall()
    for volume_id, label, uuid_, created_at in rows:
        if uuid_ in known_uuids or lifecycle.burn_evidence(volume_id) is not None:
            continue
        if not lifecycle.burn_unknown(volume_id):
            add_event(
                conn, volume_id, "NOTE",
                detail=(
                    f"{REBUILD_UNKNOWN_NOTE_PREFIX}: this catalog was "
                    f"rebuilt from discs, and none of them shows whether "
                    f"{label} was burned"
                ),
                event_date=created_at,
                commit=False,
            )
        result.burn_unknown.append(label)
    conn.commit()


def _existing_labels(output_db: Path) -> dict[str, tuple[str, str]]:
    """label -> (uuid, where) of the volumes an existing output catalog
    holds, read without writing it."""
    conn = sqlite3.connect(
        "file:" + urllib.parse.quote(str(Path(output_db).resolve())) + "?mode=ro",
        uri=True,
    )
    try:
        return {
            str(label): (str(uuid_), f"the catalog at {output_db}")
            for label, uuid_ in conn.execute("SELECT label, uuid FROM volumes")
        }
    except sqlite3.Error:
        return {}  # a new or damaged output: _open_output reports it
    finally:
        conn.close()


def rebuild_catalog(
    disc_paths: list[Path],
    output_db: Path,
) -> RebuildResult:
    """Merge the disc catalogs from *disc_paths* into *output_db*.

    Each entry in *disc_paths* should be a mounted LCSAS disc directory
    containing a ``catalog.db`` file.  The output catalog is created if it does
    not already exist (useful for building a fresh master catalog from scratch),
    or data is merged into the existing file.

    Conflict resolution (recency-aware, FMA-06):
    - Discs are merged NEWEST-first.  Freshness per disc is the newest
      timestamp in the rows of its ``catalog.db`` (volumes, discs, events,
      burn sessions, splits); the file mtime only breaks a tie (#686).
      The merge result does not depend on the order discs are listed.
    - Records with the same natural key keep the freshest catalog's view
      (newest-first ordering; a row already there is kept).  No row is
      dropped by a constraint: a value a CHECK refuses is an error for
      that disc, never an ignored row (#963).
    - Recorded password splits (``key_escrow``) follow the freshest
      catalog that knows the repository, including a split cleared since
      an older disc was staged (#573, #686).
    - Volume status follows the freshest catalog that mentions the volume,
      including downgrades to DEPRECATED/DESTROYED.  A stale disc claiming
      a volume is more alive than the freshest record (the resurrection
      hazard) is reported in :attr:`RebuildResult.warnings`.
    - A volume whose own disc is one of the inputs (its uuid is in the
      disc's ``volume_info.json``) and is still STAGING/BURNING after the
      merge becomes BURNED and is listed in
      :attr:`RebuildResult.needs_verify` (#678).
    - A volume the rebuild brought in that is still STAGING/BURNING with
      no disc record and no burn evidence gets a NOTE saying its burn
      state is unknown and is listed in
      :attr:`RebuildResult.burn_unknown` (#758).

    Sources (#963):
    - Every source is opened read-only and immutable: the rebuild never
      writes a disc catalog, nor a ``-wal``/``-journal`` file beside it.
    - Each is read before anything is merged.  One that cannot be read,
      or that holds a value this LCSAS does not know, is refused in
      words ("written by a newer LCSAS (schema vN)" for a newer schema);
      a newer one whose values all fit is merged with a warning.
    - Two volumes with one label and different uuids stop the rebuild
      before the output is touched; the error names the discs of both.
    - :attr:`RebuildResult.as_of` and :attr:`RebuildResult.newest_disc`
      say how recent the rebuilt catalog is.

    Args:
        disc_paths: List of mounted disc root directories.
        output_db: Path to the new (or existing) master catalog.

    Returns:
        :class:`RebuildResult` with merge statistics, warnings, and errors.
    """
    result = RebuildResult()

    # #963: read every source before anything is merged: its schema
    # version, whether every value fits this LCSAS, and its labels.  A
    # disc that cannot be merged is refused here, in words.
    sources: dict[Path, _Source] = {}
    for disc_path in disc_paths:
        catalog_db = disc_path / "catalog.db"
        if catalog_db.is_file():
            sources[disc_path] = _read_source(disc_path, catalog_db)
    readable = [s for s in sources.values() if not s.refusal]

    # Two volumes with one label: stop before the output is touched.
    clashes = _label_clashes(readable, {})
    if not clashes and Path(output_db).is_file():
        existing = _existing_labels(output_db)
        clashes = _label_clashes(readable, existing)
    if clashes:
        for message in clashes:
            _logger.error("%s", message)
        result.errors.extend(clashes)
        result.discs_skipped = len(disc_paths)
        return result

    conn = _open_output(output_db)

    # Volumes already present in the output catalog get a freshness
    # baseline from its own rows (the file mtime is useless here — we
    # just touched it via ensure_schema), so a genuinely fresher disc
    # can still update a stale pre-existing master.
    status_freshness: dict[str, Freshness] = {}
    escrow_freshness: dict[str, Freshness] = {}
    # #758: volumes the output catalog already had are not the rebuild's
    # to mark as of unknown burn state.
    known_uuids = {row[0] for row in conn.execute("SELECT uuid FROM volumes")}
    baseline_rows = _row_freshness(conn)
    if baseline_rows != float("-inf"):
        # No mtime for the output catalog: it wins a tie with a disc
        # whose rows are just as new (it was rebuilt from that disc, or
        # the disc was staged from it).
        baseline: Freshness = (baseline_rows, float("inf"))
        for row in conn.execute("SELECT uuid FROM volumes"):
            status_freshness[row[0]] = baseline
        for row in conn.execute(
            "SELECT repo_id, name FROM repositories"
        ).fetchall():
            escrow_freshness[row[0]] = baseline
            escrow_freshness[row[1]] = baseline
        for row in conn.execute("SELECT repo_id FROM key_escrow").fetchall():
            escrow_freshness[row[0]] = baseline

    # Sort newest-first.  Path is a deterministic tie-breaker; discs with
    # no catalog.db sort last (they only produce a skip error).
    ordered: list[tuple[Path, Path | None, Freshness]] = []
    for disc_path in disc_paths:
        source = sources.get(disc_path)
        if source is not None:
            ordered.append((disc_path, source.catalog_db, source.freshness))
        else:
            ordered.append(
                (disc_path, None, (float("-inf"), float("-inf")))
            )
    ordered.sort(
        key=lambda entry: (-entry[2][0], -entry[2][1], str(entry[0]))
    )

    is_pruned_conflicts = 0
    own_uuids: set[str] = set()
    for disc_path, source_db, freshness in ordered:
        if source_db is None:
            _logger.warning(
                "No catalog.db found at %s — skipping.", disc_path
            )
            result.discs_skipped += 1
            result.errors.append(f"No catalog.db at {disc_path}")
            continue

        source = sources[disc_path]
        if source.refusal:
            _logger.error("%s", source.refusal)
            result.discs_skipped += 1
            result.errors.append(source.refusal)
            continue

        _logger.info("Merging catalog from: %s", disc_path)
        try:
            counts = _merge_one_disc(
                conn,
                source_db,
                source_freshness=freshness,
                status_freshness=status_freshness,
                warnings=result.warnings,
                escrow_freshness=escrow_freshness,
            )
        except Exception as exc:
            # #820: a damaged disc catalog (sqlite3.DatabaseError) or any
            # other failure.  Its merge was rolled back; the later discs
            # are still merged, and the error makes 'lcsas catalog
            # rebuild' end with exit code 1.  #963: a newer disc's
            # failure says "newer", not "damaged".
            message = source.merge_failure(exc)
            _logger.error("%s", message)
            result.discs_skipped += 1
            result.errors.append(message)
            continue

        result.discs_processed += 1
        if not result.as_of:
            # Newest first: the first disc merged is the newest read.
            stamp = freshness[0] if freshness[0] != float("-inf") else freshness[1]
            result.as_of = datetime.fromtimestamp(stamp, UTC).date().isoformat()
            result.newest_disc = _disc_label(disc_path) or str(disc_path)
        if source.newer:
            result.warnings.append(
                f"{source.name} was written by a newer LCSAS (schema "
                f"v{source.version}). Every value in it fits this LCSAS and "
                f"was merged, but a newer LCSAS may record more than this one "
                f"reads: rebuild with that version if you have it."
            )
        own_uuid = _own_volume_uuid(disc_path)
        if own_uuid is not None:
            own_uuids.add(own_uuid)
        result.repositories_merged += counts.get("repositories", 0)
        result.volumes_merged += counts.get("volumes", 0)
        result.packs_merged += counts.get("packs", 0)
        result.snapshots_merged += counts.get("snapshots", 0)
        result.locations_merged += counts.get("locations", 0)
        result.events_merged += counts.get("volume_events", 0)
        result.sessions_merged += counts.get("burn_sessions", 0)
        result.session_volumes_merged += counts.get("session_volumes", 0)
        result.splits_merged += counts.get("key_escrow", 0)
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

    _promote_read_discs(conn, own_uuids, result)
    _mark_burn_unknown(conn, known_uuids, result)

    # #676: never reissue a label number that any disc carries.
    from lcsas.db.label_mark import label_seq, merge_label_high_water
    result.label_mark = merge_label_high_water(
        conn, [db for _p, db, _f in ordered if db is not None])
    for (label,) in conn.execute("SELECT label FROM volumes"):
        if label_seq(str(label)) == result.label_mark:
            result.label_mark_label = str(label)

    conn.close()
    return result
