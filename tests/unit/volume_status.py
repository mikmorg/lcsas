"""Put a volume in a status for a test (#892).

Production changes a volume's status only through the events of
``lcsas.db.lifecycle.VolumeLifecycle``
(``tests/unit/test_volume_status_writers.py``).  A test that needs a
volume in some status, without driving the event that leads there, uses
one of these two helpers; they are the only places tests write
``volumes.status``.
"""

from __future__ import annotations

import sqlite3
from typing import Any


def update_status(
    conn: sqlite3.Connection,
    volume_id: int,
    status: str,
    *,
    commit: bool = True,
    force: bool = False,
) -> None:
    """Move the volume to *status* along the status graph, as
    ``lcsas.db.volumes.update_status`` did before #892: the graph is
    checked (``ValueError``) unless *force*, DEPRECATED is refused when a
    pack would be on no other live volume, VERIFIED stamps
    ``verified_at``, and the "Status changed: X → Y" note is written."""
    from lcsas.db.lifecycle import _update_status

    _update_status(conn, volume_id, status, commit=commit, force=force)


def force_status(
    conn: sqlite3.Connection, volume_id: int, status: str, **columns: Any,
) -> None:
    """Write *status*, and any other ``volumes`` *columns* given, straight
    into the volume's row: no graph, no note, no commit.  For a state no
    event leaves, or the history of an older LCSAS."""
    names = ["status", *columns]
    conn.execute(
        f"UPDATE volumes SET {', '.join(f'{n} = ?' for n in names)} "
        f"WHERE volume_id = ?",
        (status, *columns.values(), volume_id),
    )
