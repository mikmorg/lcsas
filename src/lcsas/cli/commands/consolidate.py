"""``lcsas consolidate``."""

from __future__ import annotations

import argparse
from typing import Any

from lcsas.cli.common import _confirm, _load_config_opt, _open_catalog, _unknown_location_msg
from lcsas.log import get_logger

logger = get_logger()


def _resolve_volume_refs(
    conn: Any, refs: list[int | str],
) -> list[int] | None:
    """Catalog volume IDs for *refs*: each a volume label or an ID (#581).

    Logs and returns None if any ref matches no volume.
    """
    from lcsas.db.volumes import get_volume_by_id, get_volume_by_label

    ids: list[int] = []
    for ref in refs:
        vol = None if isinstance(ref, int) else get_volume_by_label(conn, ref)
        if vol is None and str(ref).isdigit():
            try:
                vol = get_volume_by_id(conn, int(ref))
            except ValueError:
                vol = None
        if vol is None:
            logger.error(f"Volume '{ref}' not found (give a label or an ID).")
            return None
        ids.append(vol.volume_id)
    return ids


def cmd_consolidate(args: argparse.Namespace) -> int:
    """Plan and optionally execute volume consolidation."""
    from lcsas.config.media import MediaType
    from lcsas.consolidate.merger import VolumeMerger
    from lcsas.db.volumes import get_volume_by_id

    config = _load_config_opt(args)
    with _open_catalog(args, config) as conn:
        try:
            media_type = MediaType[args.target_media]
        except KeyError:
            valid = ", ".join(m.name for m in MediaType)
            logger.error(f"Unknown media type '{args.target_media}'. "
                         f"Valid types: {valid}")
            return 1

        volume_ids = _resolve_volume_refs(conn, args.volume_ids)
        if volume_ids is None:
            return 1
        args.volume_ids = volume_ids

        reserve = config.metadata_reserve_bytes if config else 104_857_600
        merger = VolumeMerger(conn, metadata_reserve_bytes=reserve)
        plan = merger.plan_consolidation(args.volume_ids, media_type)

        logger.info("Consolidation Plan:")
        logger.info(f"  Source volumes: {', '.join(plan.source_labels)}")
        logger.info(f"  Active packs:  {len(plan.active_packs)}")
        logger.info(f"  Total size:    {plan.total_active_bytes / 1e9:.1f} GB")
        logger.info(f"  Target media:  {plan.target_media_type.name}")
        logger.info(f"  Volumes needed: {plan.volumes_needed}")
        if plan.pruned_left_behind:
            left_bytes = sum(p.size_bytes for p in plan.pruned_left_behind)
            logger.warning(
                f"  Pruned left behind: {len(plan.pruned_left_behind)} pack(s) "
                f"({left_bytes:,} bytes) on the source volume(s) are marked "
                f"pruned and will NOT migrate. If any were pruned by mistake, "
                f"run 'lcsas pack unprune <sha256>' and re-plan first:"
            )
            for p in plan.pruned_left_behind[:20]:
                logger.warning(f"    {p.sha256}")
            if len(plan.pruned_left_behind) > 20:
                logger.warning(
                    f"    … and {len(plan.pruned_left_behind) - 20} more"
                )

        # Handle --deprecate flag (separate from --execute)
        if args.deprecate:
            if args.execute:
                logger.error("--deprecate and --execute are mutually exclusive.")
                return 1
            # Mark source volumes as DEPRECATED after consolidation succeeds
            merger.deprecate_sources(args.volume_ids)
            logger.info("Marked %d source volume(s) as DEPRECATED", len(args.volume_ids))
            return 0

        # The owner and the heir know volumes by label, not catalog ID.
        source_labels = " ".join(
            get_volume_by_id(conn, vid).label for vid in args.volume_ids
        )
        if not args.execute:
            logger.info("")
            logger.info("To execute: add --execute to stage and burn.")
            logger.info("Then verify the new volumes and run: lcsas "
                        "consolidate --deprecate %s", source_labels)
            return 0

        if config is None:
            logger.error("--config is required for --execute.")
            return 1

        # #677: only VERIFIED volumes can become CONSOLIDATING.  Refuse
        # anything else now, before the prompt, not with an invalid-
        # transition error after the owner typed "yes".
        not_ready = [get_volume_by_id(conn, vid) for vid in args.volume_ids]
        not_ready = [v for v in not_ready if v.status != "VERIFIED"]
        if not_ready:
            for v in not_ready:
                hint = {
                    "BURNED": f"verify its disc first ('lcsas verify "
                              f"{v.label} --disc')",
                    "CONSOLIDATING": "it is already being consolidated: burn "
                                     "that session and run 'consolidate "
                                     "--deprecate', or 'lcsas session abort' "
                                     "it",
                }.get(v.status, "only VERIFIED volumes can be consolidated")
                logger.error(f"Volume {v.label} is {v.status}: {hint}.")
            logger.error("Nothing was staged.")
            return 1

        location = args.location or config.default_location
        if args.location:
            # A typo must not stage discs for a place that does not exist.
            from lcsas.db.locations import UnknownLocationError, resolve_location
            try:
                resolve_location(conn, location)
            except UnknownLocationError as exc:
                logger.error(_unknown_location_msg(exc))
                return 1

        # Confirmation prompt before executing irreversible staging
        logger.warning("")
        logger.warning(
            "This will stage %d packs across %d volume(s) — an irreversible catalog change.",
            len(plan.active_packs), plan.volumes_needed,
        )
        try:
            if not _confirm("Are you sure you want to proceed? Type 'yes' to confirm: "):
                logger.info("Consolidation canceled.")
                return 0
        except EOFError:
            logger.error("No terminal available for confirmation (redirected input).")
            logger.error("Run it again from a terminal so it can ask for confirmation.")
            return 1

        # Execute: stage the active packs via the burn orchestrator
        from lcsas.burn.orchestrator import BurnOrchestrator
        from lcsas.ecc.dvdisaster import SubprocessDVDisasterRunner
        from lcsas.iso.xorriso import SubprocessXorrisoRunner

        orch = BurnOrchestrator(
            config, conn,
            SubprocessXorrisoRunner(tmpdir=config.staging_path),
            SubprocessDVDisasterRunner(tmpdir=config.staging_path),
        )

        # Stage exactly the active packs from the consolidation plan
        # (#677: stage() used to intersect them with the unarchived pool,
        # which never holds packs of VERIFIED volumes).  The sources stay
        # VERIFIED until staging succeeded, so a failed stage changes
        # nothing.
        pack_shas = [p.sha256 for p in plan.active_packs]
        try:
            session = orch.stage(
                media_type=media_type,
                for_location=location,
                pack_sha256s=pack_shas,
            )
        except (ValueError, OSError) as e:
            logger.error("Staging failed: %s", e)
            logger.error("The source volumes were not changed.")
            return 1

        # Mark the sources CONSOLIDATING, tied to the new burn session:
        # 'lcsas session abort' of it returns them to VERIFIED.
        merger.mark_sources_consolidating(
            args.volume_ids, session_id=session.session_id,
        )
        logger.info("Marked %d source volume(s) as CONSOLIDATING", len(args.volume_ids))

        logger.info(f"Staged {len(session.manifests)} volume(s) for {location}.")
        logger.info(
            "Consolidation staged.  Next steps:\n"
            "  1. burn session %s\n"
            "  2. verify the new volumes (%s)\n"
            "  3. then deprecate sources with: lcsas consolidate "
            "--deprecate %s\n"
            "To cancel instead: lcsas session abort %s (the sources return "
            "to VERIFIED)",
            session.session_id,
            " ".join(m.volume_label for m in session.manifests),
            source_labels,
            session.session_id,
        )

    return 0
