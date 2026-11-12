"""``lcsas staging clean``."""

from __future__ import annotations

import argparse
from pathlib import Path

from lcsas.cli.common import _confirm, _load_config_opt, _open_catalog
from lcsas.log import get_logger

logger = get_logger()


def _report_unmanaged_staging(paths: list[Path]) -> None:
    """List staging-area entries ``staging clean`` won't touch (#486)."""
    if not paths:
        return
    from lcsas.utils.fs import dir_size_bytes

    logger.warning(
        f"{len(paths)} item(s) in the staging area are not from a tracked session "
        "and are NOT removed (even with --force).  Check each one and "
        "delete it by hand if it is stale:"
    )
    for p in paths:
        size = dir_size_bytes(p) if p.is_dir() else p.lstat().st_size
        shown = f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.1f} MB"
        logger.warning(f"  {p}  ({shown})")


def cmd_staging_clean(args: argparse.Namespace) -> int:
    """Detect and remove orphaned directories in the staging area."""
    from lcsas.staging.cleanup import (
        clean_orphaned_staging,
        detect_orphaned_staging,
        detect_unmanaged_staging,
    )

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for staging clean.")
        return 1
    # Hold the catalog lock across detection AND the confirm prompt so a
    # concurrent `lcsas stage` cannot commit a new in-flight session whose
    # directory we already flagged.  Blocking the stage here is the point;
    # with the loud-wait mitigation it now sees a "waiting for lock" message.
    with _open_catalog(args, config) as conn:
        orphans = detect_orphaned_staging(config, conn)
        _report_unmanaged_staging(detect_unmanaged_staging(config, conn))

        if not orphans:
            logger.info("No orphaned directories found in the staging area.")
            return 0

        logger.info(f"Found {len(orphans)} orphaned directory(ies) in the staging area:")
        for p in orphans:
            logger.info(f"  {p}")

        if not args.force:
            if not _confirm("Remove these directories? [y/N] ", "y"):
                logger.info("Aborted.")
                return 0
            # Re-check under the still-held lock: the prompt may have sat for
            # minutes, during which a session could have been committed against
            # one of these dirs.  Delete only the intersection.  (Also covers
            # two --force runs racing — re-detection is cheap.)
            still_orphaned = set(detect_orphaned_staging(config, conn))
            orphans = [p for p in orphans if p in still_orphaned]

        removed = clean_orphaned_staging(orphans)
    logger.info(f"Removed {removed} orphaned directory(ies) from the staging area.")
    return 0
