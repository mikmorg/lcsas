"""``lcsas pack unprune``."""

from __future__ import annotations

import argparse
from typing import Any

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
            "SELECT pack_id, sha256, is_pruned, repo_id FROM packs "
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
        problem = _not_in_mirror(conn, config, row)
        if problem is not None:
            logger.error("%s", problem)
            return 1
        unmark_pruned(conn, row["pack_id"])
        logger.info(f"Pack {row['sha256']} restored to the active pool.")
    return 0


def _not_in_mirror(conn: Any, config: Any, row: Any) -> str | None:
    """Why *row*'s pack cannot go back to the active pool, or None (#968).

    A pack whose file is not in its mirror would be marked pruned again by
    the next scan, and a stage could not copy it.  When the mirror is back
    (mounted again), ``lcsas scan`` returns its packs to the pool itself
    (#667).  The mirror is where :func:`~lcsas.config.mirrors.mirror_path`
    says (the config's path, else the catalog's); None when nothing says.
    """
    from lcsas.config.mirrors import mirror_path
    from lcsas.db.repos import get_repo
    from lcsas.utils.pack_layout import find_pack_file

    try:
        repo = get_repo(conn, row["repo_id"])
    except ValueError:
        return None
    where = mirror_path(config, repo)
    if where is None:
        return None
    if not where.is_dir():
        return (
            f"The mirror of '{repo.name}' is not at {where} (is it mounted?), "
            f"so pack {row['sha256']} cannot be checked. Nothing was changed. "
            "Mount the mirror and run 'lcsas scan': it returns every pack "
            "it finds there to the active pool."
        )
    if find_pack_file(where / "data", row["sha256"]) is None:
        return (
            f"Pack {row['sha256']} is not in the mirror of '{repo.name}' "
            f"({where}): the next 'lcsas scan' would mark it pruned again, "
            "and a stage could not copy it. Nothing was changed."
        )
    return None
