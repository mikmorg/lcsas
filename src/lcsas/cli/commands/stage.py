"""``lcsas stage``."""

from __future__ import annotations

import argparse
import sqlite3
from typing import TYPE_CHECKING, Any

from lcsas.cli.common import (
    _config_mirror,
    _load_config_opt,
    _open_catalog,
    _resolve_repo_names_to_ids,
    _validate_config_or_exit,
)
from lcsas.log import get_logger

if TYPE_CHECKING:
    from lcsas.config.settings import CollectionConfig, LCSASConfig

logger = get_logger()


def _sync_mirrors_before_stage(
    conn: sqlite3.Connection,
    repo_ids: list[str] | None,
    *,
    dry_run: bool,
) -> bool:
    """Register unscanned mirror packs before staging (#656).

    Returns False (stage must stop) when a mirror could not be read
    completely: unscanned packs may hide behind the unreadable paths.
    """
    from lcsas.packs.stage_sync import sync_unscanned_packs

    sync = sync_unscanned_packs(conn, repo_ids, dry_run=dry_run)
    if sync.incomplete:
        for name, errors in sync.incomplete.items():
            logger.error(
                "Could not read the whole mirror of repository '%s' "
                "(%d unreadable path(s), first: %s).",
                name, len(errors), errors[0],
            )
        logger.error(
            "Refusing to stage: the mirror may hold pack files that are not "
            "in the catalog. Fix the mount or permissions, run 'lcsas scan', "
            "then stage again."
        )
        return False
    for name, count in sync.unscanned.items():
        if dry_run:
            logger.info(
                "[DRY RUN] Repository '%s': %d pack file(s) in the mirror are "
                "not in the catalog yet. A real stage registers them first, "
                "as 'lcsas scan' does; this plan leaves them out.",
                name, count,
            )
        else:
            logger.info(
                "Repository '%s': registered %d pack file(s) from the mirror "
                "that were not in the catalog yet (as 'lcsas scan' does).",
                name, count,
            )
    for name, count in sync.missing_from_mirror.items():
        logger.warning(
            "Repository '%s': %d pack(s) in the catalog are no longer in the "
            "mirror. Run 'lcsas scan' to record prunes.", name, count,
        )
    return True


def _stage_collection_and_fill(
    args: argparse.Namespace, config: LCSASConfig,
) -> tuple[CollectionConfig | None, float]:
    """The ``--collection`` table and the minimum fill in effect (#701).

    ``--min-fill`` overrides the collection's ``min_fill_pct``.  Raises
    ValueError for an unknown collection, a flag the collection already
    sets, a collection repository without a readable password file, or a
    minimum fill outside 0-100.
    """
    from lcsas.config.settings import collection_password_errors

    collection = None
    name = getattr(args, "collection", None)
    if name:
        clash = [
            flag for flag, given in (
                ("--repo", getattr(args, "repo", None) is not None),
                ("--for-location", bool(getattr(args, "for_location", None))),
                ("--media", bool(getattr(args, "media", None))),
            ) if given
        ]
        if clash:
            raise ValueError(
                f"--collection {name} sets the repositories, the location "
                f"and the media type; do not also pass {', '.join(clash)}."
            )
        collection = config.collections.get(name)
        if collection is None:
            known = ", ".join(sorted(config.collections)) or "none"
            raise ValueError(
                f"Unknown collection '{name}': the config has no "
                f"[collections.{name}] table (its collections: {known})."
            )
        problems = collection_password_errors(config, [name])
        if problems:
            raise ValueError("; ".join(problems))
    min_fill = getattr(args, "min_fill", None)
    if min_fill is None:
        return collection, collection.min_fill_pct if collection else 0.0
    if not 0 <= min_fill <= 100:
        raise ValueError(f"--min-fill must be between 0 and 100, not {min_fill:g}")
    return collection, float(min_fill)


def _collection_catalog_check(
    conn: sqlite3.Connection,
    config: LCSASConfig,
    collection: CollectionConfig,
) -> tuple[list[str], list[str]]:
    """Catalog repo_ids of *collection*'s repositories, and its problems (#701).

    Every repository must be registered and active and the location must
    be registered.  Unlike ``--repo``, a name that does not resolve is an
    error, not a warning: staging four of five repositories would leave
    one silently off the collection's discs.
    """
    from lcsas.db.locations import UnknownLocationError, resolve_location
    from lcsas.db.repos import resolve_repo

    where = f"collection '{collection.name}'"
    problems: list[str] = []
    try:
        resolve_location(conn, collection.location)
    except UnknownLocationError as exc:
        hint = (f" (did you mean: {', '.join(exc.suggestions)}?)"
                if exc.suggestions else "")
        problems.append(
            f"{where}: location '{collection.location}' is not registered"
            f"{hint}. Add it with 'lcsas location add {collection.location}'."
        )
    repo_ids: list[str] = []
    for name in collection.repos:
        try:
            repo = resolve_repo(conn, name, _config_mirror(config, name))
        except ValueError as exc:
            problems.append(f"{where}: repo '{name}': {exc}")
            continue
        if repo is None:
            problems.append(
                f"{where}: repo '{name}' is not registered in the catalog. "
                f"Add it with 'lcsas repo add {name} <mirror_path>'."
            )
        elif repo.status != "active":
            problems.append(
                f"{where}: repo '{name}' is {repo.status} in the catalog; "
                f"take it out of the collection or 'lcsas repo activate "
                f"{name}'."
            )
        else:
            repo_ids.append(repo.repo_id)
    return repo_ids, problems


def _for_location_check(conn: sqlite3.Connection, location: str) -> str | None:
    """The error for a ``--for-location`` that is not registered, or None.

    Worded like :func:`_collection_catalog_check`, plus the registered
    locations: a typo must not stage discs for a place that does not
    exist.
    """
    from lcsas.db.locations import (
        UnknownLocationError,
        list_locations,
        resolve_location,
    )

    try:
        resolve_location(conn, location)
    except UnknownLocationError as exc:
        hint = (f" (did you mean: {', '.join(exc.suggestions)}?)"
                if exc.suggestions else "")
        known = [loc.name for loc in list_locations(conn)]
        listed = (f" Registered locations: {', '.join(known)}." if known
                  else " No location is registered yet.")
        return (
            f"--for-location: location '{location}' is not registered"
            f"{hint}.{listed} Add it with 'lcsas location add {location}'."
        )
    return None


def cmd_stage(args: argparse.Namespace) -> int:
    """Stage ISOs for deferred burning."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.config.media import MediaType
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for stage.")
        return 1
    if not _validate_config_or_exit(config):
        return 1
    try:
        collection, min_fill = _stage_collection_and_fill(args, config)
    except ValueError as e:
        logger.error("%s", e)
        return 1

    # No signal handler here: Ctrl-C raises KeyboardInterrupt, which
    # stage() already catches to delete a half-built volume (#699).
    with _open_catalog(args, config) as conn:
        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
            allow_escrow_drift=getattr(args, "allow_escrow_drift", False),
            allow_low_redundancy=getattr(
                args, "allow_low_redundancy", False,
            ),
        )

        if args.clean:
            session_ref = args.session or "latest"
            try:
                orch.clean_session(session_ref, force=args.force)
            except ValueError as e:
                logger.error("%s", e)
                return 1
            logger.info(f"Cleaned session: {session_ref}")
            return 0

        media_type = None
        if args.media:
            try:
                media_type = MediaType[args.media]
            except KeyError:
                valid = ", ".join(m.name for m in MediaType)
                logger.error(f"Unknown media type '{args.media}'. "
                             f"Valid types: {valid}")
                return 1

        for_location = args.for_location
        repo_ids: list[str] | None
        if collection is not None:
            # #701: the collection sets the location, the media type
            # and the repositories; each must exist in the catalog.
            repo_ids, problems = _collection_catalog_check(
                conn, config, collection,
            )
            if problems:
                for problem in problems:
                    logger.error("%s", problem)
                return 1
            for_location = collection.location
            media_type = collection.media_type
        else:
            if for_location is not None:
                location_problem = _for_location_check(conn, for_location)
                if location_problem is not None:
                    logger.error("%s", location_problem)
                    return 1
            try:
                repo_ids = _resolve_repo_names_to_ids(
                    conn, args.repo, config,
                )
            except ValueError as e:
                logger.error(str(e))
                return 1

        # #656: a disc carries each mirror's snapshots as they are now,
        # so the catalog must know every pack file in the mirrors first.
        if not _sync_mirrors_before_stage(
            conn, repo_ids, dry_run=getattr(args, "dry_run", False),
        ):
            return 1

        from lcsas.db.key_escrow import EscrowDriftError
        try:
            result = orch.stage(
                media_type=media_type,
                for_location=for_location,
                repo_ids=repo_ids,
                dry_run=getattr(args, "dry_run", False),
                min_fill_pct=min_fill,
                partial=getattr(args, "partial", False),
            )
        except EscrowDriftError as e:
            logger.error("%s", e)
            logger.error(
                "Refusing to stage. Pass --allow-escrow-drift to override "
                "(discs may print wrong share instructions)."
            )
            return 1

        if getattr(args, "dry_run", False):
            return 0
        if result.session_id == "":
            # #701: the only volume was below the minimum fill; stage()
            # said so.  Not an error: the packs wait for more data.
            return 0

        logger.info(f"Session: {result.session_id}")
        logger.info(f"Staged {len(result.manifests)} volume(s):")
        for m in result.manifests:
            iso_size = m.iso_path.stat().st_size if m.iso_path and m.iso_path.exists() else 0
            logger.info(
                "  %s  (%.1f GB, %d packs)",
                m.iso_path, iso_size / 1e9, len(m.selected_packs),
            )
        logger.info(f"Manifest: {result.staging_dir / 'session.json'}")
        _report_snapshot_coverage(result)
    return 0


def _report_snapshot_coverage(result: Any) -> None:
    """#657: say which snapshots these discs do NOT carry, and why.

    A snapshot goes on a disc only when every pack it needs is on a
    burned disc or on this session's discs.  The rest wait for a later
    disc; until then the newest snapshot on the discs is older than the
    newest backup.
    """
    for repo_name, snaps in sorted(result.snapshots_left_out.items()):
        logger.warning(
            "Repository '%s': %d snapshot(s) are NOT on these discs, "
            "because some of their packs are not on any disc yet:",
            repo_name, len(snaps),
        )
        for snap in snaps:
            logger.warning(
                "  %s  %s  (%d pack(s) not on a disc)",
                snap.snapshot_id[:8], snap.time, snap.missing_packs,
            )
        logger.warning(
            "  A later disc carries them once their packs are burned "
            "(run 'lcsas stage' again after these discs are burned)."
        )
    for repo_name, reason in sorted(result.snapshots_unchecked.items()):
        logger.warning(
            "Repository '%s': could not check which snapshots are fully on "
            "discs (%s); every snapshot was copied to these discs.",
            repo_name, reason,
        )
