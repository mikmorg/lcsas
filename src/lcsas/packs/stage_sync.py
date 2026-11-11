"""Bring the catalog up to date with the mirrors before staging (#656).

``lcsas stage`` copies each repository's index, snapshot and key files
from the mirror as they are at staging time, but takes its packs from
the catalog.  A backup run after the last ``lcsas scan`` would put a
snapshot on the disc whose packs are on no disc.  So stage first
registers every pack file in the mirrors that the catalog does not
know yet: the additive half of ``lcsas scan``, which is always safe.

The other half of a scan, marking packs pruned, stays with
``lcsas scan`` and its confirm threshold (BURN-09): a pack missing from
a mirror may only be unreadable.  Stage reports such packs and leaves
them alone.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from lcsas.db.repos import list_repos
from lcsas.packs.delta import DeltaAnalyzer
from lcsas.packs.scanner import scan_mirror_packs


@dataclass
class MirrorSyncResult:
    """What :func:`sync_unscanned_packs` found, per repository name."""

    # Pack files in the mirror that the catalog did not know.  Registered
    # unless the sync ran as a dry run.
    unscanned: dict[str, int] = field(default_factory=dict)
    # Repositories whose mirror could not be read completely: packs may
    # be missing from the catalog without anyone knowing.
    incomplete: dict[str, list[str]] = field(default_factory=dict)
    # Active catalog packs that are no longer in the mirror (pruned, or
    # the mirror is not all there).  Only ``lcsas scan`` acts on these.
    missing_from_mirror: dict[str, int] = field(default_factory=dict)


def sync_unscanned_packs(
    conn: sqlite3.Connection,
    repo_ids: list[str] | None = None,
    *,
    dry_run: bool = False,
) -> MirrorSyncResult:
    """Register the pack files in the mirrors that the catalog lacks.

    Covers every active catalog repository (or only *repo_ids*).  With
    *dry_run* nothing is written; the counts are still reported.
    """
    result = MirrorSyncResult()
    wanted = set(repo_ids) if repo_ids else None
    for repo in list_repos(conn):
        if repo.status != "active":
            continue
        if wanted is not None and repo.repo_id not in wanted:
            continue
        scan = scan_mirror_packs(Path(repo.mirror_path))
        if scan.errors:
            result.incomplete[repo.name] = list(scan.errors)
        analyzer = DeltaAnalyzer(conn, scan.packs, repo.repo_id)
        known = _known_shas(conn, list(scan.packs))
        new = [sha for sha in scan.packs if sha not in known]
        if new:
            result.unscanned[repo.name] = len(new)
            if not dry_run:
                analyzer.register_new_packs()
        if scan.packs and not scan.errors:
            gone = analyzer.detect_pruned()
            if gone:
                result.missing_from_mirror[repo.name] = len(gone)
    return result


def _known_shas(conn: sqlite3.Connection, shas: list[str]) -> set[str]:
    """The pack IDs among *shas* that the catalog already holds."""
    known: set[str] = set()
    batch = 900  # stay under SQLite's variable limit
    for i in range(0, len(shas), batch):
        chunk = shas[i : i + batch]
        placeholders = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"SELECT sha256 FROM packs WHERE sha256 IN ({placeholders})",
            chunk,
        ).fetchall()
        known.update(str(r["sha256"]) for r in rows)
    return known
