"""``lcsas estate card``.

The card text itself is in :mod:`lcsas.cli.main`.
"""

from __future__ import annotations

import argparse
import sys

from lcsas.cli.common import _load_config_opt, _open_catalog, _resolve_db_path
from lcsas.log import get_logger

logger = get_logger()


def cmd_estate_card(args: argparse.Namespace) -> int:
    """Generate the whole-archive estate card (UX-09).

    Reads survivability fields from the config and, when a catalog is
    reachable, the burned-disc count.  Never fails on a missing/old
    catalog — the disc inventory degrades to a fill-in blank.
    """
    from datetime import date

    # The card text is frozen in main.py; imported here, not at the top,
    # because main.py imports this module.
    from lcsas.cli.main import _estate_card_text

    config = _load_config_opt(args)
    if config is None:
        logger.error("--config is required to generate an estate card.")
        return 1

    disc_count: int | None = None
    recorded: dict[str, tuple[int, int]] = {}
    db_path = _resolve_db_path(args, config)
    if db_path.exists():
        from lcsas.constants import (
            STATUS_BURNED,
            STATUS_CONSOLIDATING,
            STATUS_DEPRECATED,
            STATUS_VERIFIED,
        )

        # #684: ensure_schema may migrate the catalog, so it runs under
        # the catalog lock like every other writer.
        with _open_catalog(args, db_path=db_path) as conn:
            # #684: count the volumes the heir can hold: burned (and
            # beyond) with at least one ACTIVE disc.  Not in-progress
            # STAGING/BURNING volumes, not DESTROYED ones, and not a
            # volume whose every disc was destroyed or retired (a lost
            # volume, Q33, or one superseded by consolidation).  Copies
            # of one volume share its label, so this counts labels.
            burned = (
                STATUS_BURNED, STATUS_VERIFIED, STATUS_CONSOLIDATING,
                STATUS_DEPRECATED,
            )
            row = conn.execute(
                "SELECT COUNT(*) FROM volumes v "
                f"WHERE v.status IN ({', '.join('?' * len(burned))}) "
                "AND EXISTS (SELECT 1 FROM volume_copies c "
                "            WHERE c.volume_id = v.volume_id "
                "              AND c.status = 'ACTIVE')",
                burned,
            ).fetchone()
            # None, not 0, before the first burn: a card printed then (or
            # from staged drill discs) leaves the fill-in blank instead of
            # telling the heir there are "0 disc(s)".
            disc_count = int(row[0]) or None
            # KEY-08 / #573: print the split actually performed, not the
            # config's K/N.
            from lcsas.db.key_escrow import recorded_splits_by_name
            recorded = recorded_splits_by_name(conn, {
                name: repo.mirror_path
                for name, repo in config.repositories.items()
            })

    from lcsas.staging.metadata import split_view

    view = split_view(config, recorded=recorded)
    uniform = view.uniform if view is not None else None
    mixed = view is not None and uniform is None
    card = _estate_card_text(
        owner=config.archive_owner,
        description=config.archive_description,
        technical_contact=config.technical_contact,
        repositories=sorted(config.repositories),
        key_storage_hints=config.key_storage_hints,
        key_split=uniform is not None,
        key_threshold=uniform[0] if uniform else config.key_threshold,
        key_shares=uniform[1] if uniform else config.key_shares,
        label_prefix=config.label_prefix,
        disc_count=disc_count,
        card_date=date.today().isoformat(),
        split_repos=view.by_repo if mixed and view is not None else None,
        unsplit_repos=view.unsplit if mixed and view is not None else (),
    )

    if args.output is not None:
        args.output.write_text(card, encoding="utf-8")
        print(f"Wrote estate card to {args.output}")
        print(
            "  Store this sheet WITH the discs; store the password "
            "SOMEWHERE ELSE.",
            file=sys.stderr,
        )
    else:
        print(card, end="")
    return 0
