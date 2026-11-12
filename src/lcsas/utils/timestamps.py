"""Catalog timestamps: one text format for new rows, tolerant readers (#730).

New rows are written as UTC ISO-8601 with an offset, to the
millisecond (``2026-11-11T10:00:00.123+00:00``).  That is the format
the Python writers already used for disc records, verify times and
events, cut to SQLite's precision: ``julianday('now')`` has
milliseconds, so a row stamped with microseconds could look newer than
"now" for the rest of that millisecond.

Catalogs written before #730, and every disc catalog already burned,
also hold SQLite ``CURRENT_TIMESTAMP`` text (``2026-11-11 10:00:00``,
naive UTC) in the columns that relied on the schema default, and burn
dates copied verbatim from receipts with any offset.  The schema keeps
its defaults (a disc catalog is never migrated), so readers must accept
both forms.  As text the two do not sort or compare correctly (a space
sorts before ``T``, and offsets differ), so:

- SQL that orders or compares timestamps wraps the column in
  ``julianday()``, which reads both forms and any offset.
- Python reads them with :func:`parse_utc` (a value without a zone is
  UTC).
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> str:
    """The current time as the catalog writes it: UTC ISO-8601, ms."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def parse_utc(value: object) -> datetime | None:
    """A catalog or receipt timestamp as an aware UTC datetime.

    Accepts ISO-8601 with ``T`` or a space, with or without an offset (a
    value without one is UTC, as ``CURRENT_TIMESTAMP`` writes it).
    Returns None for anything else (not a string, empty, unreadable).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        when = datetime.fromisoformat(value.strip())
    except ValueError:
        return None
    if when.tzinfo is None:
        return when.replace(tzinfo=UTC)
    return when.astimezone(UTC)


def to_utc_iso(value: object) -> str | None:
    """*value* rewritten in the catalog's format, or None if unreadable."""
    when = parse_utc(value)
    return when.isoformat() if when is not None else None


def same_instant(a: object, b: object) -> bool:
    """True when *a* and *b* are the same text, or name the same instant."""
    if a == b:
        return True
    ta, tb = parse_utc(a), parse_utc(b)
    return ta is not None and ta == tb
