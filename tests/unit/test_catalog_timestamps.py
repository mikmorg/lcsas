"""#730: one text format for new catalog timestamps; readers accept both.

New rows are written as UTC ISO-8601 to the millisecond (``utc_now()``).
Catalogs written before, and every disc catalog already burned, keep
SQLite ``CURRENT_TIMESTAMP`` text (``2026-11-11 10:00:00``) and receipt
dates with other offsets.  As text these sort wrongly (a space sorts
before ``T``), so code that orders or compares them reads them as
instants.
"""

from __future__ import annotations

import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db import rebuild
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location, ensure_location
from lcsas.db.packs import bulk_register, register_pack
from lcsas.db.queries import get_ghost_volumes
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import create_session, get_latest_session
from lcsas.db.volume_copies import add_volume_copy, find_stale_copies
from lcsas.db.volume_events import add_event, get_events_for_volume
from lcsas.db.volumes import create_volume
from lcsas.utils.timestamps import parse_utc, same_instant, to_utc_iso

UTC_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3}|\.\d{6})?\+00:00")


# ---------------------------------------------------------------------------
# The helpers
# ---------------------------------------------------------------------------


def test_parse_utc_reads_both_forms_and_offsets():
    ten = datetime(2026, 11, 11, 10, 0, tzinfo=UTC)
    assert parse_utc("2026-11-11 10:00:00") == ten
    assert parse_utc("2026-11-11T10:00:00+00:00") == ten
    assert parse_utc("2026-11-11T12:00:00+02:00") == ten
    assert parse_utc("2026-11-11T10:00:00Z") == ten
    assert parse_utc("") is None
    assert parse_utc("yesterday") is None
    assert parse_utc(None) is None


def test_to_utc_iso_and_same_instant():
    assert to_utc_iso("2026-11-11T12:00:00+02:00") == "2026-11-11T10:00:00+00:00"
    assert to_utc_iso("2026-11-11 10:00:00") == "2026-11-11T10:00:00+00:00"
    canonical = "2026-11-11T10:00:00.123456+00:00"
    assert to_utc_iso(canonical) == canonical  # unchanged
    assert to_utc_iso("not a date") is None
    assert same_instant("2026-11-11T12:00:00+02:00", "2026-11-11 10:00:00")
    assert not same_instant("2026-11-11T10:00:01+00:00", "2026-11-11 10:00:00")
    assert same_instant("odd", "odd")


# ---------------------------------------------------------------------------
# Writers: every new row in one format
# ---------------------------------------------------------------------------


def test_new_rows_use_one_format(conn):
    register_repo(conn, "r", "R", "/m")
    create_location(conn, "Loc_A")
    ensure_location(conn, "Loc_B")
    register_pack(conn, "a" * 64, 1, "r")
    bulk_register(conn, [("b" * 64, 1, "r")])
    vol = create_volume(conn, "V1", "u1", "TEST_TINY", 1000, "Loc_A")
    create_session(conn, "TEST_TINY", "/staging", session_id="s1")
    add_event(conn, vol.volume_id, "NOTE", detail="now")
    add_event(conn, vol.volume_id, "NOTE", detail="offset",
              event_date="2026-11-11T12:00:00+02:00")
    add_volume_copy(conn, vol.volume_id, "Loc_A",
                    burn_date="2026-11-11T12:00:00+02:00",
                    last_verified_at="2026-11-11 10:00:00")

    values = [
        *(r[0] for r in conn.execute("SELECT created_at FROM repositories")),
        *(r[0] for r in conn.execute("SELECT created_at FROM locations")),
        *(r[0] for r in conn.execute("SELECT created_at FROM packs")),
        *(r[0] for r in conn.execute("SELECT created_at FROM volumes")),
        *(r[0] for r in conn.execute("SELECT created_at FROM burn_sessions")),
        *(r[0] for r in conn.execute("SELECT event_date FROM volume_events")),
        *(r[0] for r in conn.execute(
            "SELECT burn_date FROM volume_copies "
            "UNION ALL SELECT last_verified_at FROM volume_copies")),
    ]
    assert len(values) == 11
    bad = [v for v in values if not UTC_ISO.fullmatch(v)]
    assert bad == []
    # A value with another offset is the same instant, rewritten in UTC.
    assert "2026-11-11T10:00:00+00:00" in values


def test_unreadable_event_date_is_kept_as_given(conn):
    vol = create_volume(conn, "V1", "u1", "TEST_TINY", 1000)
    ev = add_event(conn, vol.volume_id, "NOTE", event_date="unknown")
    assert ev.event_date == "unknown"


# ---------------------------------------------------------------------------
# Readers: both forms, ordered and compared as instants
# ---------------------------------------------------------------------------


def _raw_event(conn, vid: int, when: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO volume_events (volume_id, event_type, event_date, detail) "
        "VALUES (?, 'NOTE', ?, ?)", (vid, when, detail),
    )
    conn.commit()


def test_events_newest_first_across_forms(conn):
    vol = create_volume(conn, "V1", "u1", "TEST_TINY", 1000)
    # Old CURRENT_TIMESTAMP row at 10:00, newer-code row at 09:00 and one
    # at 10:30 written with a +02:00 offset (a receipt's burn date).
    _raw_event(conn, vol.volume_id, "2026-11-11 10:00:00", "old-10:00")
    _raw_event(conn, vol.volume_id, "2026-11-11T09:00:00+00:00", "new-09:00")
    _raw_event(conn, vol.volume_id, "2026-11-11T12:30:00+02:00", "rcpt-10:30")
    order = [e.detail for e in get_events_for_volume(conn, vol.volume_id)]
    assert order == ["rcpt-10:30", "old-10:00", "new-09:00"]


def test_latest_session_across_forms(conn):
    for sid, when in (("old", "2026-11-11 10:00:00"),
                      ("new", "2026-11-11T09:00:00+00:00")):
        conn.execute(
            "INSERT INTO burn_sessions (session_id, media_type, staging_dir, "
            "created_at) VALUES (?, 'TEST_TINY', '/s', ?)", (sid, when),
        )
    conn.commit()
    latest = get_latest_session(conn)
    assert latest is not None and latest.session_id == "old"


def test_ghost_cutoff_reads_new_format(conn):
    """A volume staged two hours ago, written in the new format, is older
    than a one-hour cutoff (as text, 'T' sorted after the cutoff's space
    on the same day)."""
    old = create_volume(conn, "V_OLD_FORM", "u1", "TEST_TINY", 1000)
    new = create_volume(conn, "V_NEW_FORM", "u2", "TEST_TINY", 1000)
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    conn.execute("UPDATE volumes SET created_at = ? WHERE volume_id = ?",
                 (two_hours_ago.strftime("%Y-%m-%d %H:%M:%S"), old.volume_id))
    conn.execute("UPDATE volumes SET created_at = ? WHERE volume_id = ?",
                 (two_hours_ago.isoformat(), new.volume_id))
    conn.commit()
    ghosts = {v.label for v in get_ghost_volumes(conn, older_than_hours=1)}
    assert ghosts == {"V_OLD_FORM", "V_NEW_FORM"}
    # A volume staged just now is not older than an hour, in either form.
    create_volume(conn, "V_NOW", "u3", "TEST_TINY", 1000)
    assert "V_NOW" not in {
        v.label for v in get_ghost_volumes(conn, older_than_hours=1)
    }


def _raw_copy(conn, label: str, location: str, last_verified: str) -> None:
    vol = create_volume(conn, label, f"uuid-{label}", "TEST_TINY", 1000,
                        location, "VERIFIED")
    ensure_location(conn, location)
    conn.execute(
        "INSERT INTO volume_copies (volume_id, location, burn_date, "
        "last_verified_at) VALUES (?, ?, ?, ?)",
        (vol.volume_id, location, "2024-01-01T00:00:00+00:00", last_verified),
    )
    conn.commit()


def test_stale_cutoff_across_forms(conn):
    now = datetime(2026, 11, 12, tzinfo=UTC)  # cutoff 2025-11-12T00:00Z
    # 2025-11-12T04:00Z written with -05:00: inside the year, not stale.
    _raw_copy(conn, "V_OFFSET", "L1", "2025-11-11T23:00:00-05:00")
    # 2025-11-12 01:00 UTC, old form: inside the year, not stale.
    _raw_copy(conn, "V_OLD_FRESH", "L2", "2025-11-12 01:00:00")
    # 2025-11-11 23:00 UTC, old form: stale.
    _raw_copy(conn, "V_OLD_STALE", "L3", "2025-11-11 23:00:00")
    # 2025-10-01 UTC, new form: stale, and older than the one above.
    _raw_copy(conn, "V_NEW_STALE", "L4", "2025-10-01T00:00:00+00:00")
    stale = [label for label, _loc, _when in
             find_stale_copies(conn, older_than_days=365, now=now)]
    assert stale == ["V_NEW_STALE", "V_OLD_STALE"]


def test_status_stale_copies_reads_old_form(tmp_path, capsys):
    """`status --stale-copies` and `volume impact` print the age of a disc
    verified in the old (no offset) form instead of failing on it."""
    db = tmp_path / "c.db"
    c = get_connection(db)
    create_all(c)
    _raw_copy(c, "V_OLD", "Home", "2020-01-01 10:00:00")
    c.close()
    assert main(["--db", str(db), "status", "--stale-copies"]) == 0
    out = capsys.readouterr().out
    assert "V_OLD" in out and "(2020-01-01)" in out
    assert main(["--db", str(db), "volume", "impact", "V_OLD"]) == 0
    assert "(2020-01-01)" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Receipts: stored in UTC, still seen as imported
# ---------------------------------------------------------------------------


@pytest.fixture
def receipt_env(tmp_path: Path) -> tuple[Path, Path]:
    staging = tmp_path / "staging"
    staging.mkdir()
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{tmp_path}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    c = get_connection(db)
    create_all(c)
    ensure_location(c, "Offsite")
    vol = create_volume(c, "VOL_R", "uuid-r", "TEST_TINY", 1000, "Offsite",
                        "STAGING")
    create_session(c, "TEST_TINY", str(staging), session_id="s1")
    from lcsas.db.sessions import add_session_volume
    add_session_volume(c, "s1", vol.volume_id, str(staging / "VOL_R.iso"),
                       iso_sha256="a" * 64, iso_size_bytes=2048)
    c.commit()
    c.close()
    return cfg, db


def test_receipt_date_stored_in_utc_and_replay_seen(receipt_env, tmp_path,
                                                    capsys):
    cfg, db = receipt_env
    receipt = tmp_path / "r.json"
    receipt.write_text(json.dumps({
        "volume_label": "VOL_R", "location": "Offsite", "session_id": "s1",
        "device": "/dev/sr0", "burn_date": "2026-04-14T14:00:00+02:00",
        "iso_sha256": "a" * 64, "iso_size_bytes": 2048,
        "verify_passed": True,
    }))
    argv = ["--config", str(cfg), "catalog", "import-receipts", str(receipt)]
    assert main(argv) == 0
    c = get_connection(db)
    try:
        burn_date = c.execute("SELECT burn_date FROM volume_copies").fetchone()[0]
        events_before = c.execute("SELECT COUNT(*) FROM volume_events").fetchone()[0]
    finally:
        c.close()
    assert burn_date == "2026-04-14T12:00:00+00:00"
    capsys.readouterr()

    assert main(argv) == 0
    assert "already imported" in capsys.readouterr().out
    c = get_connection(db)
    try:
        assert c.execute(
            "SELECT COUNT(*) FROM volume_events").fetchone()[0] == events_before
    finally:
        c.close()


# ---------------------------------------------------------------------------
# Rebuild: the newest instant of a mixed column
# ---------------------------------------------------------------------------


def _catalog_with_events(base: Path, name: str, dates: list[str]) -> Path:
    d = base / name
    d.mkdir()
    db = d / "catalog.db"
    c = get_connection(db)
    create_all(c)
    c.execute(
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes, "
        "status, created_at) VALUES ('V', ?, 'BD25', 1, 'VERIFIED', "
        "'2026-01-01 00:00:00')", (f"uuid-{name}",),
    )
    for i, when in enumerate(dates):
        c.execute(
            "INSERT INTO volume_events (volume_id, event_type, event_date, "
            "detail) VALUES (1, 'NOTE', ?, ?)", (when, str(i)),
        )
    c.commit()
    c.close()
    os.utime(db, (1_700_000_000, 1_700_000_000))
    return db


def test_rebuild_freshness_reads_the_newest_instant(tmp_path):
    # Its newest row is 23:00 (old form); as text 'T01:00' sorts last.
    a = _catalog_with_events(tmp_path, "a", ["2026-11-11 23:00:00",
                                             "2026-11-11T01:00:00+00:00"])
    b = _catalog_with_events(tmp_path, "b", ["2026-11-11T12:00:00+00:00"])
    assert rebuild._source_freshness(a) > rebuild._source_freshness(b)


def test_utc_now_is_not_newer_than_sqlite_now(conn):
    """A row stamped now is never after SQLite's 'now' (ms precision).

    With microseconds, a volume created in the same millisecond as the
    query looked newer than julianday('now'), so a zero-hour ghost
    cutoff missed it (CI flake in test_ghost_volumes_and_drift).
    """
    from lcsas.utils.timestamps import utc_now

    for _ in range(200):
        stamp = utc_now()
        row = conn.execute(
            "SELECT julianday(?) <= julianday('now')", (stamp,),
        ).fetchone()
        assert row[0] == 1, stamp
