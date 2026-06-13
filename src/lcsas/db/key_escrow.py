"""CRUD for the key_escrow table — durable record of `lcsas key split` (KEY-08).

Whether burned discs print share-recovery instructions, and the K/N they
claim, used to come solely from hand-edited ``lcsas.toml`` fields.  This table
records the split that was *actually* performed so disc text can be derived
from it and the burn pipeline can abort on config drift.

The table is optional on read paths: it arrived at schema v9, and v≤8 disc
catalogs (and any catalog opened before ``migrate()`` runs) may lack it.
:func:`get_split` tolerates the missing table so restore/rebuild code never
crashes on an old catalog.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from lcsas.db.models import KeyEscrow


def _table_exists(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='key_escrow'"
    ).fetchone()
    return row is not None


def _row_to_escrow(row: sqlite3.Row) -> KeyEscrow:
    return KeyEscrow(
        repo_id=row["repo_id"],
        threshold=row["threshold"],
        shares=row["shares"],
        slip39_id=row["slip39_id"],
        split_at=row["split_at"],
    )


def record_split(
    conn: sqlite3.Connection,
    repo_id: str,
    threshold: int,
    shares: int,
    slip39_id: int,
    *,
    split_at: str | None = None,
    commit: bool = True,
) -> KeyEscrow:
    """Record (or replace, on rotation) the split state for *repo_id*.

    Replace-on-conflict means re-splitting a repo overwrites the prior
    record — the newest split is the one heirs should follow.
    """
    if split_at is None:
        split_at = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO key_escrow
               (repo_id, threshold, shares, slip39_id, split_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(repo_id) DO UPDATE SET
               threshold = excluded.threshold,
               shares    = excluded.shares,
               slip39_id = excluded.slip39_id,
               split_at  = excluded.split_at""",
        (repo_id, threshold, shares, slip39_id, split_at),
    )
    if commit:
        conn.commit()
    got = get_split(conn, repo_id)
    assert got is not None  # just inserted  # noqa: S101
    return got


def get_split(conn: sqlite3.Connection, repo_id: str) -> KeyEscrow | None:
    """Return the recorded split for *repo_id*, or None.

    Tolerates a missing ``key_escrow`` table (v≤8 / pre-migration catalogs):
    returns None rather than raising, so read paths stay safe on old shapes.
    """
    if not _table_exists(conn):
        return None
    row = conn.execute(
        "SELECT * FROM key_escrow WHERE repo_id = ?", (repo_id,)
    ).fetchone()
    return _row_to_escrow(row) if row else None


def clear_split(
    conn: sqlite3.Connection, repo_id: str, *, commit: bool = True
) -> None:
    """Delete the recorded split for *repo_id* (no-op if none / no table)."""
    if not _table_exists(conn):
        return
    conn.execute("DELETE FROM key_escrow WHERE repo_id = ?", (repo_id,))
    if commit:
        conn.commit()


class EscrowDriftError(RuntimeError):
    """Config key_split/K/N disagrees with the recorded split (KEY-08).

    Burning would print share instructions that contradict the split that was
    actually performed — permanently, on discs read decades later.  The
    message names BOTH sides of the disagreement.
    """


def detect_escrow_drift(
    conn: sqlite3.Connection,
    repo_ids: list[str],
    *,
    config_split: bool,
    config_threshold: int,
    config_shares: int,
) -> str | None:
    """Return an actionable drift message, or None if config and records agree.

    The TOML carries one archive-wide ``key_split``/``key_threshold``/
    ``key_shares``; splits are recorded per repo.  For each repo being burned:

    - config says split, no record (or K/N mismatch) → drift.
    - record exists, config says ``key_split=false`` → drift (the heir would
      be told to find a single key that was superseded by the split).

    A repo with neither a record nor ``key_split`` is consistent (no split).
    """
    for repo_id in sorted(set(repo_ids)):
        record = get_split(conn, repo_id)
        if config_split:
            if record is None:
                return (
                    f"key escrow drift for repo '{repo_id}': lcsas.toml says "
                    f"key_split=true {config_threshold}-of-{config_shares} but "
                    f"the catalog records no split for this repo. Discs would "
                    f"print instructions to gather share cards that were never "
                    f"made. Re-run 'lcsas key split --config ... --repo "
                    f"{repo_id}' or set key_split=false under [defaults]."
                )
            if (record.threshold, record.shares) != (
                config_threshold, config_shares
            ):
                return (
                    f"key escrow drift for repo '{repo_id}': lcsas.toml says "
                    f"key_split=true {config_threshold}-of-{config_shares} but "
                    f"the catalog records a {record.threshold}-of-"
                    f"{record.shares} split on {record.split_at}. Discs would "
                    f"print wrong instructions. Re-run 'lcsas key split' or fix "
                    f"key_threshold/key_shares under [defaults]."
                )
        else:
            if record is not None:
                return (
                    f"key escrow drift for repo '{repo_id}': lcsas.toml says "
                    f"key_split=false but the catalog records a "
                    f"{record.threshold}-of-{record.shares} split on "
                    f"{record.split_at}. Discs would tell the heir to find a "
                    f"single key that was superseded by that split. Set "
                    f"key_split=true (key_threshold={record.threshold}, "
                    f"key_shares={record.shares}) under [defaults], or run "
                    f"'lcsas key combine' to undo the split."
                )
    return None
