"""``lcsas restore``: plan, exec, standalone.

The handlers parse arguments, open the catalog and print.  The restore
itself is in :mod:`lcsas.restore.orchestrator`.
"""

from __future__ import annotations

import argparse

from lcsas.cli.common import (
    _load_config_opt,
    _open_catalog,
    _validate_config_or_exit,
)
from lcsas.log import get_logger
from lcsas.restore.orchestrator import (
    PlannedRestore,
    plan_restore,
    prepare_restore,
    restore_from_disc,
    run_restore,
)

logger = get_logger()


def cmd_restore_plan(args: argparse.Namespace) -> int:
    """Generate a restore pick list for a snapshot."""
    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for restore plan.")
        return 1
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    with _open_catalog(args, config, mode="read") as conn:
        planned = plan_restore(
            config, conn, repo_name=args.repo, snapshot_id=args.snapshot_id,
        )
    if planned is None:
        return 1
    return _log_pick_list(args, planned)


def _log_pick_list(args: argparse.Namespace, planned: PlannedRestore) -> int:
    """Print the ``restore plan`` pick list; 1 when packs are missing."""
    pick_list = planned.pick_list
    logger.info(f"Restore Pick List for snapshot {args.snapshot_id}")
    logger.info(f"  Repository: {args.repo}")
    logger.info(f"  Required packs: {len(planned.plan.required_pack_hashes)}")
    logger.info("")

    if pick_list.volumes:
        for label, sources in sorted(pick_list.volumes.items()):
            total = sum(s.pack.size_bytes for s in sources)
            also_on = sorted({
                alt for s in sources for alt in s.alternates if alt != label
            })
            also = f"  also on: {', '.join(also_on)}" if also_on else ""
            logger.info(f"  {label:<30} {len(sources):>4} packs  "
                       f"({total / (1024 * 1024):.1f} MB){also}")
        logger.info("")
        logger.info(f"  Total: {pick_list.total_packs} packs across "
                   f"{len(pick_list.volumes)} volumes "
                   f"({pick_list.total_bytes / (1024 * 1024):.1f} MB)")

    if pick_list.deprecated_disc_labels:
        logger.warning(
            "\n  WARNING: %d pack(s) are only available on DEPRECATED or "
            "DESTROYED volumes. These discs may still be physically "
            "retrievable if you have kept them.",
            sum(len(v) for v in pick_list.deprecated_disc_labels.values()),
        )
        for label, hashes in sorted(pick_list.deprecated_disc_labels.items()):
            logger.warning(
                "    %s  (%d pack(s))", label, len(hashes)
            )
        logger.warning(
            "  If you can locate these discs, mount them and re-run restore."
        )

    for label, hashes in sorted(pick_list.unconfirmed_volume_labels.items()):
        logger.warning(
            "\n  WARNING: volume %s was staged but has no record of ever "
            "being burned. If you cannot find this disc, it may never have "
            "existed. The %d pack(s) it lists may only exist on the "
            "mirror.",
            label, len(hashes),
        )

    if pick_list.missing_packs:
        logger.error(
            "\n  ERROR: %d pack(s) required for this snapshot are not found in "
            "any archived volume. Restore is impossible without these packs.",
            len(pick_list.missing_packs),
        )
        for sha in pick_list.missing_packs[:10]:
            logger.error(f"    missing: {sha}")
        if len(pick_list.missing_packs) > 10:
            logger.error(
                "    ... and %d more missing packs",
                len(pick_list.missing_packs) - 10,
            )
        logger.error(
            "  If discs are physically present but not yet scanned, run "
            "`lcsas catalog validate /mnt/disc` to check each disc."
        )
        return 1

    return 0


def cmd_restore_exec(args: argparse.Namespace) -> int:
    """Execute a restore operation."""
    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for restore exec.")
        return 1
    if not _validate_config_or_exit(config, skip_staging=True):
        return 1
    with _open_catalog(args, config, mode="read") as conn:
        planned = prepare_restore(
            config, conn,
            repo_name=args.repo,
            snapshot_id=args.snapshot_id,
            password_file=args.password_file,
        )
    if planned is None:
        return 1

    return run_restore(
        planned,
        staging_path=config.staging_path,
        snapshot_id=args.snapshot_id,
        target_path=args.target_path,
        password_file=args.password_file,
        cache_dir=args.cache_dir,
        volume_dir=args.volume_dir,
        # Only the interactive path reads --iso-dir.
        iso_dir=getattr(args, "iso_dir", None),
        skip_verify=args.skip_verify,
    )


def cmd_restore_from_disc(args: argparse.Namespace) -> int:
    """Restore from optical discs without a config file or mirror.

    Reads ``catalog.db`` and repository metadata directly from a mounted
    or extracted disc.  No ``--config``, local mirror, or master
    catalog required — everything needed is embedded on each disc.

    Typical usage::

        lcsas restore standalone /mnt/disc1 ~/restored/ \\
            --password-file ~/secret.key

    For scripted restores with pre-extracted discs::

        lcsas restore standalone /tmp/disc1 ~/restored/ \\
            --password-file ~/secret.key \\
            --volume-dir /tmp/extracted_discs/
    """
    return restore_from_disc(
        args.disc,
        args.target_path,
        args.password_file,
        catalog=args.catalog,
        repo=args.repo,
        snapshot=args.snapshot,
        cache_dir=args.cache_dir,
        volume_dir=args.volume_dir,
        skip_verify=args.skip_verify,
    )
