"""``lcsas scan``."""

from __future__ import annotations

import argparse

from lcsas.cli.common import _load_config_opt, _open_catalog, _validate_config_or_exit
from lcsas.log import get_logger

logger = get_logger()


def cmd_scan(args: argparse.Namespace) -> int:
    """Scan mirrors for new packs and register them in the catalog."""
    import json as _json

    from lcsas.db.queries import get_archive_status_summary
    from lcsas.db.repos import resolve_repo
    from lcsas.packs.delta import DeltaAnalyzer
    from lcsas.packs.scanner import scan_mirror_packs

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for scan.")
        return 1
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    with _open_catalog(args, config) as conn:
        # Config key → catalog repo_id, filled as each repository resolves.
        repos_db: dict[str, str] = {}
        repo_filter = set(args.repo) if args.repo else None
        total_new = 0
        total_scanned = 0
        repos_scanned = 0
        repos_unregistered: list[str] = []
        snapshot_failures: list[str] = []

        # An unknown --repo name is a typo, not a skip: scanning the rest and
        # exiting 0 hid it (#516).
        if repo_filter:
            unknown = repo_filter - set(config.repositories.keys())
            if unknown:
                for name in sorted(unknown):
                    logger.error(f"repository '{name}' not found in config.")
                logger.error(
                    "Configured repositories: "
                    f"{', '.join(sorted(config.repositories)) or '(none)'}"
                )
                return 1

        for repo_name, repo_cfg in config.repositories.items():
            if repo_filter and repo_name not in repo_filter:
                continue

            # Config key → catalog repo_id: mirror path, then name (#687).
            try:
                db_repo = resolve_repo(conn, repo_name, repo_cfg.mirror_path)
            except ValueError as exc:
                logger.error("%s", exc)
                return 1
            db_repo_id = db_repo.repo_id if db_repo is not None else None
            if db_repo_id is None:
                logger.warning(
                    f"repository '{repo_name}' not registered in the catalog "
                    f"(run 'repo add' first), skipping."
                )
                repos_unregistered.append(repo_name)
                continue
            repos_db[repo_name] = db_repo_id

            mirror_path = repo_cfg.mirror_path
            scan_result = scan_mirror_packs(mirror_path)
            packs_on_disk = scan_result.packs
            total_scanned += len(packs_on_disk)
            repos_scanned += 1

            analyzer = DeltaAnalyzer(conn, packs_on_disk, db_repo_id)
            new_packs = analyzer.register_new_packs()
            # #667: a pack an earlier scan marked pruned but this scan found
            # is in the mirror: back to the active pool.  Like registration,
            # this is additive and safe on an incomplete scan.
            unpruned = analyzer.unprune_found()

            total_new += len(new_packs)

            # Prune sync: detect packs removed by rustic prune (BURN-09:
            # only on a complete scan, and never en masse unconfirmed —
            # registration above is additive and always safe).
            if not getattr(args, "no_prune_sync", False):
                if scan_result.errors:
                    logger.warning(
                        f"    Scan of {repo_name} was INCOMPLETE "
                        f"({len(scan_result.errors)} unreadable path(s)) — "
                        f"prune-sync skipped for this repository. "
                        f"Fix permissions/mount and re-scan."
                    )
                else:
                    pruned = analyzer.detect_pruned()
                    if pruned:
                        from lcsas.db.packs import bulk_mark_pruned, list_packs
                        active_total = len(list_packs(
                            conn, repo_id=db_repo_id, include_pruned=False,
                        ))
                        if (len(pruned) > max(10, 0.2 * active_total)
                                and not getattr(args, "yes_prune", False)):
                            logger.warning(
                                f"    Refusing to mark {len(pruned)}/{active_total} "
                                f"packs of {repo_name} pruned in one scan — this "
                                f"usually means the mirror is partially "
                                f"unavailable. Re-run with --yes-prune to confirm "
                                f"rustic really pruned them."
                            )
                        else:
                            pruned_ids = [p.pack_id for p in pruned]
                            pruned_bytes = sum(p.size_bytes for p in pruned)
                            marked = bulk_mark_pruned(conn, pruned_ids)
                            logger.info(
                                f"    Pruned packs:   {marked} "
                                f"({pruned_bytes:,} bytes)"
                            )

            # Counted after un-prune and prune-sync, so the number is the
            # one `lcsas stage` will see (#696 item 9).
            unarchived = analyzer.get_unarchived()
            unarchived_bytes = analyzer.get_total_unarchived_bytes()

            logger.info(f"  {repo_name}:")
            logger.info(f"    Packs on disk:  {len(packs_on_disk)}")
            logger.info(f"    Newly registered: {len(new_packs)}")
            if unpruned:
                logger.info(
                    f"    Un-pruned:      {len(unpruned)} (found in the "
                    f"mirror again)"
                )
            logger.info(f"    Unarchived:     {len(unarchived)} ({unarchived_bytes:,} bytes)")

        # Persist snapshots (unless --no-snapshots)
        if not getattr(args, "no_snapshots", False):
            from lcsas.db.models import Snapshot
            from lcsas.db.snapshots import bulk_upsert_snapshots, delete_blank_snapshots
            from lcsas.exceptions import BinaryError
            from lcsas.rustic.wrapper import SubprocessRusticRunner
            from lcsas.utils.subprocess import check_binary_version

            try:
                check_binary_version("rustic", min_version=(0, 9, 0))
            except BinaryError as exc:
                logger.error("%s", exc)
                return 1

            runner = SubprocessRusticRunner(tmpdir=config.staging_path)
            total_snaps = 0
            junk = delete_blank_snapshots(conn)
            if junk:
                logger.info(
                    f"  Removed {junk} snapshot row(s) with an empty id left "
                    "by an earlier scan (#536)."
                )

            for repo_name, repo_cfg in config.repositories.items():
                if repo_filter and repo_name not in repo_filter:
                    continue
                if repo_cfg.password_file is None:
                    logger.debug(
                        f"  {repo_name}: no password_file configured, "
                        f"skipping snapshot listing"
                    )
                    continue

                repo_id = repos_db.get(repo_name)
                if repo_id is None:
                    logger.warning(
                        f"  {repo_name}: not registered in the catalog, skipping snapshots"
                    )
                    continue

                try:
                    snap_infos = runner.snapshots(
                        repo_path=repo_cfg.mirror_path,
                        password_file=repo_cfg.password_file,
                    )
                except Exception as exc:
                    logger.warning(
                        f"  {repo_name}: snapshot listing failed: {exc}"
                    )
                    snapshot_failures.append(repo_name)
                    continue
                db_snaps = [
                    Snapshot(
                        snapshot_id=si.snapshot_id,
                        repo_id=repo_id,
                        hostname=si.hostname,
                        timestamp=si.timestamp,
                        paths=_json.dumps(si.paths),
                        tags=_json.dumps(si.tags),
                        description="",
                    )
                    for si in snap_infos
                ]
                count = bulk_upsert_snapshots(conn, db_snaps)
                total_snaps += count

            if total_snaps:
                logger.info(f"  Snapshots persisted: {total_snaps}")

        summary = get_archive_status_summary(conn)

    # Count what was scanned, not what is configured (#516).
    skipped = ""
    if repos_unregistered:
        skipped = (f" ({len(repos_unregistered)} skipped, not registered: "
                   f"{', '.join(repos_unregistered)})")
    logger.info(f"\nTotal scanned: {total_scanned} packs across "
               f"{repos_scanned} repo(s){skipped}")
    if snapshot_failures:
        # Pack registration, the scan's job, succeeded, so this stays a
        # warning; but say so at the end, where it is read.
        logger.warning(
            f"Snapshot listing failed for: {', '.join(snapshot_failures)} "
            "(packs were still registered; see the warnings above)."
        )
    logger.info(f"New packs registered: {total_new}")
    logger.info(f"Archive: {summary['total']} total, "
               f"{summary['archived']} archived, "
               f"{summary['staged']} staged, "
               f"{summary['unarchived']} unarchived")
    return 0
