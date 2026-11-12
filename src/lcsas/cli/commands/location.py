"""``lcsas location``."""

from __future__ import annotations

import argparse
from typing import Any

from lcsas.cli.common import _load_config_opt, _open_catalog
from lcsas.log import get_logger

logger = get_logger()


def cmd_location(args: argparse.Namespace) -> int:
    """Handle location subcommands."""
    from lcsas.utils.labels import sanitize_name

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required for location.")
        return 1

    with _open_catalog(args, config) as conn:
        if args.location_command == "list":
            from lcsas.db.locations import list_locations
            from lcsas.db.queries import get_location_summary

            locations = list_locations(conn)
            if not locations:
                logger.info("No locations registered.")
                return 0

            summaries = get_location_summary(conn)
            summary_map = {s["location"]: s for s in summaries}

            for loc in locations:
                s = summary_map.get(loc.name, {"volumes": 0, "packs": 0, "missing": 0})
                status = (
                    "all current" if s["missing"] == 0
                    else f"{s['missing']} packs behind"
                )
                logger.info(
                    "  %-20s %d volumes, %d packs, %s",
                    loc.name, s["volumes"], s["packs"], status,
                )

        elif args.location_command == "add":
            from lcsas.db.locations import create_location
            name = sanitize_name(args.name, "location name")
            create_location(conn, name, args.description)
            logger.info(f"Added location: {name}")

        elif args.location_command == "status":
            from lcsas.db.locations import get_location
            from lcsas.db.queries import get_packs_at_location, get_packs_missing_at_location

            # An unknown name used to print "0 archived / 0 missing", which
            # reads as "a location that holds nothing" (#491).
            try:
                get_location(conn, args.name)
            except ValueError as exc:
                logger.error(f"{exc}. See `lcsas location list`.")
                return 1

            at_loc = get_packs_at_location(conn, args.name)
            missing = get_packs_missing_at_location(conn, args.name)

            logger.info(f"Location: {args.name}")
            logger.info(f"  Packs archived here: {len(at_loc)}")
            logger.info(f"  Packs missing: {len(missing)}")
            if missing:
                # Group by repository
                by_repo: dict[str, list[Any]] = {}
                for p in missing:
                    repo = p.repo_id or "unknown"
                    by_repo.setdefault(repo, []).append(p)
                for repo, packs in sorted(by_repo.items()):
                    total_size = sum(p.size_bytes for p in packs)
                    logger.info(f"    repo={repo}: {len(packs)} packs ({total_size / 1e9:.1f} GB)")

        elif args.location_command == "rename":
            from lcsas.db.locations import rename_location
            new_name = sanitize_name(args.new_name, "location name")
            try:
                moved = rename_location(conn, args.old_name, new_name)
            except ValueError as exc:
                logger.error(str(exc))
                return 1
            logger.info(
                f"Renamed location {args.old_name} → {new_name} "
                f"({moved} disc, event and volume record(s) updated)."
            )

        elif args.location_command == "remove":
            from lcsas.db.locations import delete_location
            try:
                delete_location(conn, args.name)
            except ValueError as exc:
                logger.error(str(exc))
                return 1
            logger.info(f"Removed location: {args.name}")

        elif args.location_command == "move":
            from lcsas.db.volume_copies import move_volume_copy
            from lcsas.db.volumes import get_volume_by_label

            vol = get_volume_by_label(conn, args.volume_label)
            if vol is None:
                logger.error(f"Volume '{args.volume_label}' not found.")
                return 1
            move_volume_copy(conn, vol.volume_id, args.from_location, args.to_location)
            logger.info(f"Moved {args.volume_label}: {args.from_location} → {args.to_location}")

        else:
            logger.error(
                "Usage: lcsas location {list|add|status|rename|remove|move}"
            )
            return 1
    return 0
