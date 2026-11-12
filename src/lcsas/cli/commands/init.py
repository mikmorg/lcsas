"""``lcsas init``."""

from __future__ import annotations

import argparse
from pathlib import Path

from lcsas.cli.common import _open_catalog, _resolve_db_path
from lcsas.log import get_logger

logger = get_logger()


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize the LCSAS catalog.

    Resolution order for the catalog path:
      1. ``--db-path`` on ``init`` (when provided)
      2. global ``--db``
      3. ``paths.database`` from the TOML config (``--config``)
      4. ``archive.db`` in the current working directory
    """

    # --db-path is init-only and outranks everything; the rest of the
    # ladder is the same one every other command walks.
    db_path: Path | None = getattr(args, "db_path", None)
    if db_path is None:
        db_path = _resolve_db_path(args)

    # Ensure parent directory exists (XDG paths may not exist yet)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_catalog(args, db_path=db_path):
        pass  # opening it creates and migrates the catalog
    logger.info(f"Initialized the LCSAS catalog at {db_path}")
    return 0
