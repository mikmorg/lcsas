"""Volume label high-water mark: a label number is never issued twice (#676).

Labels end in a sequence number (``LCSAS_BD25_2026_0007``).  Taking the
highest number among the catalog's volumes plus one reissues a number
once its volume is gone (``lcsas session abort``, a volume delete), so a
disc burned from the first ISO and a disc of the new volume could carry
one label.

The catalog therefore remembers the highest number ever issued, in the
SQLite header field ``PRAGMA user_version``.  That is not a schema change:
no table or column changes, the schema version stays as it is, and no
reader of the catalog looks at the field.  It survives every row delete,
travels in each disc catalog (a copy of the file), and a catalog rebuilt
from discs takes the highest mark and label it finds (``db/rebuild.py``).
"""

from __future__ import annotations

import sqlite3
import urllib.parse
from pathlib import Path

from lcsas.utils.labels import next_seq_num


def label_seq(label: str) -> int | None:
    """The sequence number at the end of a volume label, or None."""
    parts = label.split("_")
    if len(parts) < 4:
        return None
    try:
        return int(parts[-1])
    except ValueError:
        return None


def label_high_water(conn: sqlite3.Connection, schema: str = "main") -> int:
    """The highest label number ever issued in this catalog (0 if none)."""
    row = conn.execute(f"PRAGMA {schema}.user_version").fetchone()
    return int(row[0]) if row is not None else 0


def raise_label_high_water(conn: sqlite3.Connection, seq: int) -> None:
    """Record that label number *seq* was issued.  Never lowers the mark.

    Runs inside the caller's open transaction, so it commits (or rolls
    back) together with the volume row that took the number.
    """
    if seq > label_high_water(conn):
        conn.execute(f"PRAGMA user_version = {int(seq)}")


def next_label_seq(conn: sqlite3.Connection, prefix: str) -> int:
    """The number for the next new volume label: above every label in the
    catalog and above every number ever issued."""
    labels = [str(r[0]) for r in conn.execute("SELECT label FROM volumes")]
    return max(next_seq_num(labels, prefix), label_high_water(conn) + 1)


def merge_label_high_water(
    conn: sqlite3.Connection, source_dbs: list[Path],
) -> int:
    """Set *conn*'s mark to at least the highest mark and label number in
    *conn* and in each disc catalog in *source_dbs*.  Returns the mark.

    Used by a catalog rebuild from discs.  An unreadable source is
    skipped; the rebuild reports it separately.
    """
    mark = label_high_water(conn)
    for (label,) in conn.execute("SELECT label FROM volumes"):
        mark = max(mark, label_seq(str(label)) or 0)
    for db in source_dbs:
        try:
            # Percent-encoded, as in rebuild.py: mount paths may hold '?',
            # '#' or spaces.
            uri = "file:" + urllib.parse.quote(str(db)) + "?mode=ro"
            src = sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            continue
        try:
            mark = max(mark, label_high_water(src))
            for (label,) in src.execute("SELECT label FROM volumes"):
                mark = max(mark, label_seq(str(label)) or 0)
        except sqlite3.Error:
            pass
        finally:
            src.close()
    if mark > label_high_water(conn):
        conn.execute(f"PRAGMA user_version = {int(mark)}")
        conn.commit()
    return mark
