"""``lcsas copy``: deprecate, destroy."""

from __future__ import annotations

import argparse
import sqlite3
from typing import TYPE_CHECKING

from lcsas.cli.common import _load_config_opt, _open_catalog, _resolve_db_path
from lcsas.log import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from lcsas.db.models import Pack

logger = get_logger()


# How many pack IDs `lcsas copy` lists per group before "... and N more".
_LOST_PACK_LIST_LIMIT = 20


def _log_pack_ids(
    log: Callable[..., None], packs: list[Pack], limit: int | None,
) -> None:
    """Log one pack ID per line, at most *limit* of them (None = all)."""
    shown = packs if limit is None else packs[:limit]
    for p in shown:
        log("      %s", p.sha256)
    if len(packs) > len(shown):
        log("      ... and %d more", len(packs) - len(shown))


def _print_lost_volume_packs(
    conn: sqlite3.Connection,
    label: str,
    volume_id: int,
    *,
    limit: int | None = _LOST_PACK_LIST_LIMIT,
) -> None:
    """Tell the owner what a lost volume leaves on no disc (Q33).

    Two groups, by repository: packs now only in the mirror (the next
    ``lcsas stage`` burns them again) and, loudly, packs pruned from the
    mirror too, which neither the mirror nor any disc in service holds.
    """
    from lcsas.db.queries import get_lost_volume_packs
    from lcsas.db.repos import list_repos

    left = get_lost_volume_packs(conn, volume_id)
    repo_names = {r.repo_id: r.name for r in list_repos(conn)}

    def by_repo(packs: list[Pack]) -> dict[str, list[Pack]]:
        groups: dict[str, list[Pack]] = {}
        for p in packs:
            groups.setdefault(repo_names.get(p.repo_id, p.repo_id), []).append(p)
        return groups

    if not left.mirror_only and not left.gone:
        logger.info(
            "  Every pack on %s is also on another volume in service; "
            "nothing needs burning again.", label,
        )
        return

    if left.mirror_only:
        logger.warning(
            "  %d pack(s) (%.2f GB) from %s are now on no disc in service, "
            "only in the mirror. The next 'lcsas stage' will burn them "
            "again onto a new volume:",
            len(left.mirror_only),
            sum(p.size_bytes for p in left.mirror_only) / 1e9,
            label,
        )
        for repo, packs in sorted(by_repo(left.mirror_only).items()):
            logger.warning(
                "    repo %s: %d pack(s) (%.2f GB)",
                repo, len(packs), sum(p.size_bytes for p in packs) / 1e9,
            )
            _log_pack_ids(logger.warning, packs, limit)

    if left.gone:
        logger.error(
            "DATA LOSS: %d pack(s) from %s were pruned from the mirror and "
            "are on no other volume in service, so neither the mirror nor "
            "any disc in service holds them. An older snapshot that still "
            "needs one (kept only in older disc catalogs) can no longer be "
            "fully restored:",
            len(left.gone), label,
        )
        for repo, packs in sorted(by_repo(left.gone).items()):
            logger.error("    repo %s: %d pack(s)", repo, len(packs))
            _log_pack_ids(logger.error, packs, limit)


def cmd_copy(args: argparse.Namespace) -> int:
    """Record a disc as deprecated, destroyed or lost (BURN-10, Q33, Q38).

    This is the supported way to tell the catalog a disc is gone.  When
    a volume's last in-service disc is gone the volume becomes a lost
    volume: the catalog records it as DEPRECATED (no LOST status exists,
    ADR 0003), so the redundancy report and the deprecation guard stop
    counting it, and its packs re-enter the next ``lcsas stage``.  The
    owner is told which packs that is, and which are gone for good.
    """
    from lcsas.db.queries import get_lost_volume_ids
    from lcsas.db.volume_copies import deprecate_copy, destroy_copy
    from lcsas.db.volumes import get_volume_by_label

    config = _load_config_opt(args)
    db_path = _resolve_db_path(args, config)
    lost = args.copy_command == "destroy" and getattr(args, "lost", False)

    with _open_catalog(args, db_path=db_path) as conn:
        vol = get_volume_by_label(conn, args.volume_label)
        if vol is None:
            logger.error(f"Volume '{args.volume_label}' not found.")
            return 1
        # Warn on the change to lost, not only on auto-deprecation: a lost
        # volume that regained a disc before #726 stayed DEPRECATED, so
        # losing that disc again demotes nothing but makes the volume lost
        # once more.  Since #726 a regained disc puts the volume back in
        # service, and losing it again demotes it.
        was_lost = vol.volume_id in get_lost_volume_ids(conn)
        try:
            if args.copy_command == "deprecate":
                demoted = deprecate_copy(conn, vol.volume_id, args.location)
                recorded = "DEPRECATED (out of service)"
            else:
                demoted = destroy_copy(
                    conn, vol.volume_id, args.location, lost=lost,
                )
                recorded = "lost (disc status DESTROYED)" if lost else "DESTROYED"
        except ValueError as exc:
            logger.error(str(exc))
            return 1
        logger.info(
            f"Recorded the disc of {args.volume_label} at {args.location} "
            f"as {recorded}."
        )
        if lost:
            logger.warning(
                "A lost disc may be in a stranger's hands. Like every data "
                "disc, it carries the disc catalog in plain text and every "
                "repository's locked key files, so whoever has it can try "
                "to guess your passwords offline. Only a strong password "
                "protects the data on it. See docs/DISC_CONFIDENTIALITY.md."
            )
        now_lost = vol.volume_id in get_lost_volume_ids(conn)
        if demoted or (now_lost and not was_lost):
            logger.warning(
                "WARNING: %s is now a lost volume: no disc of it is left in "
                "service. The catalog records it as DEPRECATED; 'lcsas "
                "status' shows it as LOST.",
                args.volume_label,
            )
            _print_lost_volume_packs(conn, args.volume_label, vol.volume_id)
            logger.info(
                "  To also bring %s back up to date, run: "
                "lcsas stage --for-location %s",
                args.location, args.location,
            )
    return 0
