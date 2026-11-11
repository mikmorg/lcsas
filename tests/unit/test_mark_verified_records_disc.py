"""``lcsas verify <label> --mark-verified`` on a volume with no disc on
record (#673).

It used to set VERIFIED with no ``volume_copies`` row and no ``closed_at``,
so the catalog said "verified" while no disc was recorded anywhere.  Now it
needs ``--location`` there, and then records the disc, closes the volume and
writes VERIFY_PASS, as an imported receipt does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import ensure_location
from lcsas.db.models import Volume, VolumeCopy, VolumeEvent
from lcsas.db.schema import create_all
from lcsas.db.sessions import add_session_volume, create_session
from lcsas.db.volume_copies import add_volume_copy, get_copies_for_volume
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import create_volume, get_volume_by_label

_HASH = "ab" * 32


def _catalog(db: Path, status: str, *, copy_at: str | None = None) -> None:
    conn = get_connection(db)
    try:
        create_all(conn)
        vol = create_volume(conn, "VOL_MV", "u1", "BD25", 25_000_000_000,
                            "Home", status)
        create_session(conn, media_type="BD25", staging_dir="/staging",
                       session_id="sess1")
        add_session_volume(conn, "sess1", vol.volume_id,
                           iso_path="/staging/sess1/VOL_MV.iso",
                           iso_sha256=_HASH, iso_size_bytes=4096)
        if copy_at is not None:
            ensure_location(conn, copy_at)
            add_volume_copy(conn, vol.volume_id, copy_at)
    finally:
        conn.close()


def _state(db: Path) -> tuple[Volume, list[VolumeCopy], list[VolumeEvent]]:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, "VOL_MV")
        assert vol is not None
        copies = get_copies_for_volume(conn, vol.volume_id)
        passes = get_events_for_volume(conn, vol.volume_id, "VERIFY_PASS")
        return vol, copies, passes
    finally:
        conn.close()


@pytest.mark.parametrize("status", ["STAGING", "BURNED"])
def test_no_disc_and_no_location_is_refused(tmp_path, capsys, status):
    db = tmp_path / "c.db"
    _catalog(db, status)

    assert main(["--db", str(db), "verify", "VOL_MV", "--mark-verified"]) == 1

    assert "--location" in capsys.readouterr().out
    vol, copies, passes = _state(db)
    assert vol.status == status
    assert copies == [] and passes == []


@pytest.mark.parametrize("status", ["STAGING", "BURNED"])
def test_with_location_the_disc_is_recorded(tmp_path, status):
    db = tmp_path / "c.db"
    _catalog(db, status)

    assert main(["--db", str(db), "verify", "VOL_MV", "--mark-verified",
                 "--location", "Offsite_Safe",
                 "--detail", "remote burn, receipt lost"]) == 0

    vol, copies, passes = _state(db)
    assert vol.status == "VERIFIED"
    assert vol.closed_at is not None
    (copy,) = copies
    assert copy.location == "Offsite_Safe"
    assert copy.status == "ACTIVE"
    # The staged ISO's hash travels to the disc row, so a later
    # `verify --disc` / SHA-256 fallback has something to compare with.
    assert copy.iso_sha256 == _HASH
    assert copy.iso_size_bytes == 4096
    # Nothing read this disc: it is not stamped as freshly verified.
    assert copy.last_verified_at is None
    (passed,) = passes
    assert passed.location == "Offsite_Safe"
    assert passed.detail == "remote burn, receipt lost"
    conn = get_connection(db)
    try:
        (loc,) = conn.execute(
            "SELECT name FROM locations WHERE name = 'Offsite_Safe'").fetchall()
    finally:
        conn.close()
    assert loc["name"] == "Offsite_Safe"


def test_burned_volume_with_a_disc_needs_no_location(tmp_path):
    """The old path is unchanged when a disc is already on record."""
    db = tmp_path / "c.db"
    _catalog(db, "BURNED", copy_at="Home")

    assert main(["--db", str(db), "verify", "VOL_MV", "--mark-verified"]) == 0

    vol, copies, passes = _state(db)
    assert vol.status == "VERIFIED"
    assert [c.location for c in copies] == ["Home"]
    assert len(passes) == 1


def test_location_without_a_disc_there_is_refused(tmp_path, capsys):
    """--mark-verified records a disc only for a volume with none; a disc at
    another location comes in through its receipt."""
    db = tmp_path / "c.db"
    _catalog(db, "BURNED", copy_at="Home")

    assert main(["--db", str(db), "verify", "VOL_MV", "--mark-verified",
                 "--location", "Offsite_Safe"]) == 1

    assert "not at Offsite_Safe" in capsys.readouterr().out
    vol, copies, passes = _state(db)
    assert vol.status == "BURNED"
    assert [c.location for c in copies] == ["Home"]
    assert passes == []
