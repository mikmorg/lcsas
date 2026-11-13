"""#755: ``catalog rebuild`` of an older DEPRECATED view and a newer
VERIFIED view of the same volume.

A lost volume (DEPRECATED, its disc at Home recorded DESTROYED) that was
burned again (VERIFIED, a new ACTIVE disc at Home) appears in two kinds
of disc catalog: ones burned while it was lost and ones burned after the
re-burn.  The rebuild must take the newer view whatever order the discs
are fed in: status VERIFIED, the ACTIVE disc record, and no warning about
an older catalog.  The reverse (older VERIFIED, newer DEPRECATED) keeps
DEPRECATED and the retired disc record.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from lcsas.db import rebuild, schema
from lcsas.db.connection import get_connection

UUID = "uuid-755"
LABEL = "VOL755"
CREATED = "2026-01-01 00:00:00"
FIRST_BURN = "2026-01-02T10:00:00+00:00"
LOST_AT = "2026-03-01T10:00:00+00:00"
REBURN = "2026-05-01T10:00:00+00:00"


def _disc(base: Path, name: str, *, status: str, copy_status: str,
          burn_date: str, newest: str, mtime: float) -> Path:
    """A disc dir whose catalog.db records VOL755 as *status* with one
    disc record at Home (*copy_status*, *burn_date*).  *newest* is the
    newest event in the catalog: what makes it newer or older."""
    disc_dir = base / name
    disc_dir.mkdir()
    db = disc_dir / "catalog.db"
    conn = get_connection(db)
    schema.create_all(conn)
    conn.execute(
        "INSERT INTO repositories (repo_id, name, mirror_path, created_at) "
        "VALUES ('repo1', 'Test', '/mnt/mirror', ?)", (CREATED,),
    )
    conn.execute("INSERT INTO locations (name, created_at) VALUES ('Home', ?)",
                 (CREATED,))
    conn.execute(
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
        "status, created_at, closed_at, verified_at) "
        "VALUES (?, ?, 'BD25', 25000000000, ?, ?, ?, ?)",
        (LABEL, UUID, status, CREATED, FIRST_BURN, FIRST_BURN),
    )
    vid = conn.execute("SELECT volume_id FROM volumes").fetchone()[0]
    conn.execute(
        "INSERT INTO volume_copies (volume_id, location, status, burn_date, "
        "iso_sha256, last_verified_at) VALUES (?, 'Home', ?, ?, ?, ?)",
        (vid, copy_status, burn_date, "c" * 64, burn_date),
    )
    conn.execute(
        "INSERT INTO volume_events (volume_id, event_type, event_date, "
        "location, detail) VALUES (?, 'NOTE', ?, 'Home', ?)",
        (vid, newest, f"catalog {name}"),
    )
    conn.commit()
    conn.close()
    os.utime(db, (mtime, mtime))
    return disc_dir


def _rebuilt(output_db: Path) -> tuple[str, list[tuple[str, str, str]]]:
    conn = get_connection(output_db)
    try:
        status = conn.execute(
            "SELECT status FROM volumes WHERE uuid = ?", (UUID,)
        ).fetchone()[0]
        copies = [
            (r["location"], r["status"], r["burn_date"])
            for r in conn.execute(
                "SELECT vc.location, vc.status, vc.burn_date "
                "FROM volume_copies vc JOIN volumes v USING (volume_id) "
                "WHERE v.uuid = ?", (UUID,),
            )
        ]
        return status, copies
    finally:
        conn.close()


@pytest.fixture
def lost_then_reburned(tmp_path: Path) -> tuple[Path, Path]:
    # The mtimes disagree with the rows on purpose: the rows decide (#686).
    older = _disc(tmp_path, "older", status="DEPRECATED",
                  copy_status="DESTROYED", burn_date=FIRST_BURN,
                  newest=LOST_AT, mtime=1_790_000_000.0)
    newer = _disc(tmp_path, "newer", status="VERIFIED",
                  copy_status="ACTIVE", burn_date=REBURN,
                  newest=REBURN, mtime=1_780_000_000.0)
    return older, newer


@pytest.mark.parametrize("order", ["older-first", "newer-first"])
def test_older_deprecated_newer_verified(lost_then_reburned, tmp_path, order):
    older, newer = lost_then_reburned
    feed = [older, newer] if order == "older-first" else [newer, older]
    out = tmp_path / f"master-{order}.db"

    result = rebuild.rebuild_catalog(feed, out)

    assert result.ok, result.errors
    status, copies = _rebuilt(out)
    assert status == "VERIFIED"
    # The newer disc record: the re-burned ACTIVE disc, not the
    # destroyed one.
    assert copies == [("Home", "ACTIVE", REBURN)]
    # A stale DEPRECATED is less alive: nothing to warn about.
    assert not [w for w in result.warnings if LABEL in w]


@pytest.mark.parametrize("order", ["older-first", "newer-first"])
def test_older_verified_newer_deprecated(tmp_path, order):
    older = _disc(tmp_path, "older", status="VERIFIED",
                  copy_status="ACTIVE", burn_date=FIRST_BURN,
                  newest=FIRST_BURN, mtime=1_790_000_000.0)
    newer = _disc(tmp_path, "newer", status="DEPRECATED",
                  copy_status="DESTROYED", burn_date=FIRST_BURN,
                  newest=LOST_AT, mtime=1_780_000_000.0)
    feed = [older, newer] if order == "older-first" else [newer, older]
    out = tmp_path / f"master-{order}.db"

    result = rebuild.rebuild_catalog(feed, out)

    assert result.ok, result.errors
    status, copies = _rebuilt(out)
    assert status == "DEPRECATED"
    assert copies == [("Home", "DESTROYED", FIRST_BURN)]
    # The older catalog claims it is more alive: one warning names it.
    named = [w for w in result.warnings if LABEL in w]
    assert len(named) == 1
    assert "VERIFIED" in named[0] and "DEPRECATED" in named[0]


def test_both_orders_give_the_same_catalog(lost_then_reburned, tmp_path):
    older, newer = lost_then_reburned
    dumps = []
    for i, feed in enumerate(([older, newer], [newer, older])):
        out = tmp_path / f"m{i}.db"
        assert rebuild.rebuild_catalog(feed, out).ok
        conn = get_connection(out)
        try:
            dumps.append([
                tuple(r) for r in conn.execute(
                    "SELECT v.label, v.status, vc.location, vc.status, "
                    "vc.burn_date, vc.last_verified_at FROM volumes v "
                    "LEFT JOIN volume_copies vc USING (volume_id) ORDER BY 1, 3"
                )
            ])
        finally:
            conn.close()
    assert dumps[0] == dumps[1]
