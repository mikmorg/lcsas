"""``lcsas init``."""

from __future__ import annotations

import argparse
from pathlib import Path

from lcsas.cli.common import _load_config_opt, _open_catalog, _resolve_db_path
from lcsas.log import get_logger

logger = get_logger()


def cmd_init(args: argparse.Namespace) -> int:
    """Initialize the LCSAS catalog.

    Resolution order for the catalog path:
      1. ``--catalog-path`` on ``init`` (when provided)
      2. global ``--catalog``
      3. ``[paths] catalog`` from the TOML config (``--config``)
      4. ``catalog.db`` in the current working directory

    With a config, its ``[defaults] location`` is registered too, as
    ``location add`` would (#968): plain ``stage`` stages for it, and
    ``burn`` and ``stage --collection`` refuse an unregistered location.
    Running ``init`` again registers a default the config has changed to.
    """

    # --catalog-path is init-only and outranks everything; the rest of the
    # ladder is the same one every other command walks.
    config = _load_config_opt(args)
    db_path: Path | None = getattr(args, "db_path", None)
    if db_path is None:
        db_path = _resolve_db_path(args, config)

    # Ensure parent directory exists (XDG paths may not exist yet)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _open_catalog(args, db_path=db_path) as conn:
        # Opening it creates and migrates the catalog.
        if config is not None:
            from lcsas.db.locations import ensure_location, get_location
            from lcsas.utils.labels import sanitize_name

            try:
                name = sanitize_name(
                    config.default_location, "[defaults] location",
                )
            except ValueError as exc:
                logger.info(f"Initialized the LCSAS catalog at {db_path}")
                logger.error(f"{exc} Fix it in the config, then run "
                             "'lcsas init' again.")
                return 1
            try:
                get_location(conn, name)
            except ValueError:
                ensure_location(conn, name)
                logger.info(
                    f"Registered the default location from the config: {name}"
                )
    logger.info(f"Initialized the LCSAS catalog at {db_path}")
    return 0
