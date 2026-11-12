"""``lcsas session``: list, abort."""

from __future__ import annotations

import argparse

from lcsas.cli.common import _load_config_opt, _open_catalog
from lcsas.log import get_logger

logger = get_logger()


def cmd_session_list(args: argparse.Namespace) -> int:
    """List burn sessions stored in the catalog."""
    from lcsas.db.sessions import get_session_volumes, list_sessions

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for session list.")
        return 1
    with _open_catalog(args, config, mode="unlocked") as conn:
        sessions = list_sessions(conn, status_filter=args.status)

        if not sessions:
            status_filter = f" with status={args.status}" if args.status else ""
            logger.info("No sessions found%s.", status_filter)
            return 0

        logger.info("%-36s  %-10s  %-10s  %s", "SESSION ID", "STATUS", "MEDIA", "CREATED")
        logger.info("-" * 80)

        # #748: the owner and heir know volumes by label, not catalog id.
        labels = {
            int(r[0]): str(r[1])
            for r in conn.execute("SELECT volume_id, label FROM volumes")
        }
        for s in sessions:
            vols = get_session_volumes(conn, s.session_id)
            vol_labels = ", ".join(
                labels.get(sv.volume_id, f"volume {sv.volume_id}")
                for sv in vols
            )
            logger.info(
                "%-36s  %-10s  %-10s  %s",
                s.session_id, s.status, s.media_type,
                s.created_at[:19] if s.created_at else "",
            )
            if vols:
                logger.info("  volumes(%d): %s", len(vols), vol_labels)

    return 0


def cmd_session_abort(args: argparse.Namespace) -> int:
    """Abort a never-burned burn session (or one stranded volume), reclaiming packs."""
    from lcsas.burn.orchestrator import BurnOrchestrator
    from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
    from lcsas.iso.xorriso import SubprocessXorrisoRunner

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for session abort.")
        return 1

    with _open_catalog(args, config) as conn:
        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )
        try:
            never_burned = getattr(args, "never_burned", False)
            if args.volume:
                orch.abort_volume(args.volume, never_burned=never_burned)
            else:
                from lcsas.consolidate.merger import (
                    release_consolidation_sources,
                )
                from lcsas.db.sessions import resolve_session_id
                session_id = resolve_session_id(conn, args.ref)
                orch.abort_session(session_id, never_burned=never_burned)
                # #677: a consolidation session's sources go back to
                # VERIFIED; nothing is left to finish them.
                released = release_consolidation_sources(conn, session_id)
                if released:
                    logger.info(
                        "Returned %d consolidation source volume(s) to "
                        "VERIFIED: %s", len(released), ", ".join(released),
                    )
        except ValueError as e:
            logger.error("%s", e)
            return 1
    return 0
