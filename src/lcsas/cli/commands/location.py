"""``lcsas location``."""

from __future__ import annotations

import argparse
from typing import Any

from lcsas.cli.common import _load_config_opt, _open_catalog
from lcsas.log import get_logger

logger = get_logger()


def _packs_on_no_disc(conn: Any) -> int:
    """Packs on no disc at all: staged on a volume not yet burned, or on
    no volume in service (``status``'s staged and unarchived buckets)."""
    from lcsas.db.queries import get_archive_status_summary

    summary = get_archive_status_summary(conn)
    return int(summary["staged"] or 0) + int(summary["unarchived"] or 0)


def _log_packs_on_no_disc(conn: Any, indent: str = "") -> None:
    """#968: "all current" and "Packs missing: 0" compare a location with
    the burned packs only; say what is on no disc at all."""
    n = _packs_on_no_disc(conn)
    if n:
        logger.info(
            f"{indent}{n} pack(s) are on no disc at all, at any location "
            "('lcsas status' shows them)."
        )


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

            on_no_disc = _packs_on_no_disc(conn)
            for loc in locations:
                s = summary_map.get(loc.name, {"volumes": 0, "packs": 0, "missing": 0})
                if s["missing"]:
                    status = f"{s['missing']} packs behind"
                elif on_no_disc:
                    status = "holds every burned pack"
                else:
                    status = "all current"
                logger.info(
                    "  %-20s %d volumes, %d packs, %s",
                    loc.name, s["volumes"], s["packs"], status,
                )
            _log_packs_on_no_disc(conn)

        elif args.location_command == "add":
            from lcsas.db.locations import create_location, get_location
            name = sanitize_name(args.name, "location name")
            # #968: init registers the config's default location, so the
            # documented "location add <it>" may find it there already.
            try:
                get_location(conn, name)
            except ValueError:
                create_location(conn, name, args.description)
                logger.info(f"Added location: {name}")
            else:
                logger.info(f"Location {name} is already registered.")

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
                from lcsas.db.repos import list_repos

                # Group by repository, named as the owner knows it (#968).
                names = {r.repo_id: r.name for r in list_repos(conn)}
                by_repo: dict[str, list[Any]] = {}
                for p in missing:
                    repo = names.get(p.repo_id, p.repo_id or "unknown")
                    by_repo.setdefault(repo, []).append(p)
                for repo, packs in sorted(by_repo.items()):
                    total_size = sum(p.size_bytes for p in packs)
                    logger.info(f"    {repo}: {len(packs)} packs ({total_size / 1e9:.1f} GB)")
            _log_packs_on_no_disc(conn, "  ")

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
            # #968: the config names locations too, and the catalog cannot
            # change it: say which lines to change.
            stale = []
            if config.default_location == args.old_name:
                stale.append(f'[defaults] location = "{new_name}"')
            for cname, coll in sorted(config.collections.items()):
                if coll.location == args.old_name:
                    stale.append(
                        f'[collections.{cname}] location = "{new_name}"'
                    )
            if stale:
                logger.warning(
                    f"The config still names {args.old_name}. Change it "
                    f"in {args.config}:"
                )
                for line in stale:
                    logger.warning(f"  {line}")

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
            try:
                move_volume_copy(
                    conn, vol.volume_id, args.from_location, args.to_location
                )
            except ValueError as exc:
                logger.error(str(exc))
                return 1
            logger.info(f"Moved {args.volume_label}: {args.from_location} → {args.to_location}")

        else:
            logger.error(
                "Usage: lcsas location {list|add|status|rename|remove|move}"
            )
            return 1
    return 0
