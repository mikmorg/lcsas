"""``lcsas volume impact``."""

from __future__ import annotations

import argparse
import sqlite3
from typing import TYPE_CHECKING

from lcsas.cli.commands.copy import _print_lost_volume_packs
from lcsas.cli.common import _load_config_opt, _open_catalog
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.db.models import Pack

logger = get_logger()


def _print_snapshot_impact(
    conn: sqlite3.Connection,
    config: object | None,
    at_risk_by_repo: dict[str, set[str]],
    repo_names: dict[str, str],
) -> None:
    """List snapshots that reference at-risk packs (FMA-08 phase 2).

    Snapshot→pack mapping needs the live mirror and the repository
    password: the snapshot trees are walked with the Python reader, once
    per repository (``snapshot_packs``, the walk ``restore plan`` uses,
    #948); the catalog alone cannot provide it.  Every missing
    prerequisite degrades to a message — never an error — because the
    pack-level report already printed is complete catalog-only truth.
    """
    from lcsas.config.settings import LCSASConfig
    from lcsas.db.snapshots import list_snapshots
    from lcsas.packs.snapshot_coverage import SnapshotPacksError, snapshot_packs

    degrade = ("snapshot impact needs the live mirror — pack-level "
               "report above is catalog-only")

    if not isinstance(config, LCSASConfig):
        logger.warning("%s (pass --config).", degrade)
        return

    logger.info("")
    for repo_id in sorted(at_risk_by_repo, key=lambda r: repo_names.get(r, r)):
        at_risk_shas = at_risk_by_repo[repo_id]
        name = repo_names.get(repo_id, repo_id)
        repo_cfg = config.repositories.get(name)
        if repo_cfg is None or repo_cfg.password_file is None:
            logger.warning(
                "repo %s: %s (repository or password_file missing from "
                "--config).", name, degrade,
            )
            continue
        if not repo_cfg.mirror_path.is_dir():
            logger.warning(
                "repo %s: %s (mirror not mounted at %s).",
                name, degrade, repo_cfg.mirror_path,
            )
            continue
        snaps = list_snapshots(conn, repo_id=repo_id)
        if not snaps:
            logger.warning(
                "repo %s: no snapshots registered in the catalog — run "
                "'lcsas scan' first.", name,
            )
            continue
        try:
            walked = {
                s.snapshot_id: s for s in snapshot_packs(
                    repo_cfg.mirror_path, repo_cfg.password_file)
            }
        except SnapshotPacksError as exc:
            logger.warning(
                "repo %s: cannot tell which snapshots are affected: %s",
                name, exc,
            )
            continue
        affected = []
        unchecked = 0
        for snap in snaps:
            found = walked.get(snap.snapshot_id)
            if found is None:
                unchecked += 1
                logger.warning(
                    "repo %s snapshot %s: not in the mirror — cannot tell "
                    "whether it is affected.", name, snap.snapshot_id,
                )
                continue
            if at_risk_shas & found.packs:
                affected.append(snap)
            elif found.unreadable:
                # Some of its trees could not be read: its pack list may
                # be short, so "not affected" is not known.
                unchecked += 1
                logger.warning(
                    "repo %s snapshot %s: %d of its trees or blobs cannot be "
                    "read in the mirror — cannot tell whether it is "
                    "affected.", name, snap.snapshot_id, len(found.unreadable),
                )
        if not affected:
            if unchecked:
                logger.warning(
                    "repo %s: no snapshot that could be checked references "
                    "an at-risk pack; %d could not be checked.",
                    name, unchecked,
                )
            else:
                logger.info(
                    "repo %s: no snapshot references an at-risk pack.", name
                )
            continue
        logger.warning(
            "repo %s: %d snapshot(s) would become unrestorable:",
            name, len(affected),
        )
        for snap in affected:
            logger.warning(
                "  %s  %s  paths=%s",
                snap.snapshot_id, snap.timestamp or "<no timestamp>",
                snap.paths,
            )
        if unchecked:
            logger.warning(
                "repo %s: %d more snapshot(s) could not be checked.",
                name, unchecked,
            )


def cmd_volume_impact(args: argparse.Namespace) -> int:
    """Blast radius for one volume: what is lost if ALL its discs fail?

    Pack-level (catalog-only) report always prints; ``--snapshots`` adds
    the snapshot-level view when the live mirror is available (FMA-08).
    """
    from datetime import UTC, datetime

    from lcsas.constants import STATUS_DEPRECATED
    from lcsas.db.queries import (
        get_at_risk_packs_for_volume,
        get_lost_volume_ids,
        volume_display_status,
    )
    from lcsas.db.repos import list_repos
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volumes import get_volume_by_label

    config = _load_config_opt(args)
    with _open_catalog(args, config, mode="unlocked-migrate") as conn:
        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1

        copies = get_copies_for_volume(conn, vol.volume_id, active_only=False)
        at_risk = get_at_risk_packs_for_volume(conn, vol.volume_id)
        repo_names = {r.repo_id: r.name for r in list_repos(conn)}
        lost_ids = get_lost_volume_ids(conn)
        lost = vol.volume_id in lost_ids

        shown = volume_display_status(vol, lost_ids)
        logger.info(f"Volume {vol.label}  [{shown}]  {vol.media_type}")
        if lost:
            logger.warning(
                "  No disc of this volume is left in service: it is a lost "
                "volume (recorded as %s).", vol.status,
            )
        elif not copies:
            logger.info("  No disc of this volume is recorded in the catalog.")
        elif not any(c.status == "ACTIVE" for c in copies):
            logger.info("  No disc of this volume is left in service.")
        from lcsas.utils.timestamps import parse_utc

        now = datetime.now(UTC)
        for c in copies:
            # #730: an older or imported value may have no offset.
            when = parse_utc(c.last_verified_at)
            if when is not None:
                days = (now - when).days
                age = (f"last verified {days} day(s) ago "
                       f"({when.date().isoformat()})")
            elif c.last_verified_at:
                age = f"last verified on an unreadable date ({c.last_verified_at})"
            else:
                age = "never verified"
            logger.info(f"  disc at {c.location:<20} [{c.status}]  {age}")

        logger.info("")
        if lost and vol.status == STATUS_DEPRECATED:
            # Already happened: no blast radius left to predict, only
            # what the volume left on no disc (Q33).
            _print_lost_volume_packs(conn, vol.label, vol.volume_id, limit=None)
            return 0
        if lost:
            logger.warning(
                "  The catalog still records it as %s, so its packs still "
                "count as on a disc and 'lcsas stage' will not burn them "
                "again. 'lcsas catalog reconcile' lists this drift.",
                vol.status,
            )
        # #968: a pruned pack is not "live", but losing its last disc
        # still loses it (copy destroy says DATA LOSS): count it apart.
        pruned_only = get_at_risk_packs_for_volume(
            conn, vol.volume_id, pruned=True,
        )
        live = conn.execute(
            "SELECT COUNT(*) FROM volume_packs vp JOIN packs p "
            "USING (pack_id) WHERE vp.volume_id = ? AND p.is_pruned = 0",
            (vol.volume_id,),
        ).fetchone()[0]
        if not at_risk:
            if live:
                logger.info(
                    "Blast radius: NONE — every %spack on this volume is "
                    "also on another live volume.",
                    "live " if pruned_only else "",
                )
            else:
                logger.info(
                    "Blast radius: no live pack — every pack on this "
                    "volume was pruned from the mirror."
                )
        if pruned_only:
            logger.warning(
                "%d pruned pack(s) (%.2f GB) on this volume are on no other "
                "volume in service. No snapshot in the mirror needs them, "
                "but an older snapshot kept only in older disc catalogs may: "
                "if every disc of %s fails, it can no longer be fully "
                "restored.",
                len(pruned_only),
                sum(p.size_bytes for p in pruned_only) / 1e9, vol.label,
            )
        if not at_risk:
            return 0

        by_repo: dict[str, list[Pack]] = {}
        for p in at_risk:
            by_repo.setdefault(p.repo_id, []).append(p)
        total = sum(p.size_bytes for p in at_risk)
        logger.warning(
            "Blast radius: if every disc of %s fails, %d pack(s) "
            "(%.2f GB) become unrestorable:",
            vol.label, len(at_risk), total / 1e9,
        )
        for repo_id in sorted(by_repo, key=lambda r: repo_names.get(r, r)):
            packs = by_repo[repo_id]
            logger.warning(
                "  repo %-15s %d pack(s) (%.2f GB)",
                repo_names.get(repo_id, repo_id), len(packs),
                sum(p.size_bytes for p in packs) / 1e9,
            )

        if args.snapshots:
            _print_snapshot_impact(
                conn, config,
                {rid: {p.sha256 for p in packs}
                 for rid, packs in by_repo.items()},
                repo_names,
            )
    return 0
