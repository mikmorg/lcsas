"""``lcsas pack unprune``."""

from __future__ import annotations

import argparse

from lcsas.cli.common import _load_config_opt, _open_catalog, _resolve_db_path
from lcsas.log import get_logger

logger = get_logger()


def cmd_pack_unprune(args: argparse.Namespace) -> int:
    """Restore a wrongly-pruned pack to the active pool (BURN-09)."""
    from lcsas.db.packs import unmark_pruned

    config = _load_config_opt(args)
    db_path = _resolve_db_path(args, config)

    prefix = args.sha256.strip().lower()
    if not prefix:
        logger.error("Empty SHA-256 prefix.")
        return 1

    with _open_catalog(args, db_path=db_path) as conn:
        # substr() prefix match: immune to LIKE wildcards in typed input.
        rows = conn.execute(
            "SELECT pack_id, sha256, is_pruned FROM packs "
            "WHERE substr(sha256, 1, ?) = ? ORDER BY sha256",
            (len(prefix), prefix),
        ).fetchall()

        if not rows:
            logger.error(f"No pack matches prefix '{prefix}'.")
            return 1
        if len(rows) > 1:
            logger.error(
                f"Prefix '{prefix}' is ambiguous ({len(rows)} matches) — "
                f"give more characters:"
            )
            for row in rows[:10]:
                logger.error(f"  {row['sha256']}")
            if len(rows) > 10:
                logger.error(f"  … and {len(rows) - 10} more")
            return 1

        row = rows[0]
        if not row["is_pruned"]:
            logger.info(
                f"Pack {row['sha256']} is already active — nothing to do."
            )
            return 0
        unmark_pruned(conn, row["pack_id"])
        logger.info(f"Pack {row['sha256']} restored to the active pool.")
    return 0
