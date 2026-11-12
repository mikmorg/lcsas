"""#752: a lost volume given a disc before #726 is revived by reading it.

Before #726, a lost volume (DEPRECATED, its last disc recorded lost)
that got a disc recorded again stayed DEPRECATED with an ACTIVE disc.
The state is built here directly in SQL.  ``lcsas verify <label> --disc``
(with or without ``--location``) reads that disc; a passing check puts
the volume back in service, VERIFIED, as recording the disc would now.
A volume deprecated by consolidation stays DEPRECATED.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from lcsas.cli.main import main
from lcsas.db.locations import create_location
from lcsas.db.queries import get_lost_volume_ids
from lcsas.db.volumes import get_volume_by_id, update_status
from tests.unit.test_lost_volume_reburn import (
    _assert_in_service,
    burned,  # noqa: F401  (fixture)
)


def _pre_726_disc(conn, volume_id: int, location: str) -> None:
    """Record an ACTIVE disc the way the catalog did before #726: the
    row only, the volume left as it is."""
    conn.execute(
        "INSERT INTO volume_copies (volume_id, location, burn_date) "
        "VALUES (?, ?, ?)",
        (volume_id, location, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def _verify_disc(db, label: str, *extra: str, ok: bool = True) -> int:
    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value=label),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=ok),
        patch("lcsas.burn.device_verify.read_device_sha256",
              side_effect=lambda device, n:
              hashlib.sha256(b"\x00" * n).hexdigest()),
    ):
        return main(["--db", str(db), "verify", label, "--disc",
                     "--device", "/dev/null", *extra])


@pytest.mark.parametrize("extra", [(), ("--location", "Offsite")])
def test_verify_disc_revives_a_pre_726_lost_volume(
    burned, caplog, extra,  # noqa: F811
):
    conn = burned["conn"]
    create_location(conn, "Offsite")
    _pre_726_disc(conn, burned["volume_id"], "Offsite")
    vol = get_volume_by_id(conn, burned["volume_id"])
    assert vol.status == "DEPRECATED"
    # Not LOST either: it has an ACTIVE disc.  Nothing else revives it.
    assert vol.volume_id not in get_lost_volume_ids(conn)

    caplog.clear()
    assert _verify_disc(burned["config"].db_path, burned["label"],
                        *extra) == 0, caplog.text
    _assert_in_service(conn, burned["volume_id"], "VERIFIED")
    assert (f"{burned['label']} was a lost volume; with its disc at Offsite "
            "it is in service again (VERIFIED)") in caplog.text
    (stamp,) = conn.execute(
        "SELECT last_verified_at FROM volume_copies WHERE volume_id = ? "
        "AND location = 'Offsite'", (burned["volume_id"],),
    ).fetchone()
    assert stamp is not None


def test_failed_verify_does_not_revive(burned):  # noqa: F811
    conn = burned["conn"]
    create_location(conn, "Offsite")
    _pre_726_disc(conn, burned["volume_id"], "Offsite")
    assert _verify_disc(burned["config"].db_path, burned["label"],
                        ok=False) == 1
    assert get_volume_by_id(conn, burned["volume_id"]).status == "DEPRECATED"


def test_consolidated_volume_is_never_revived(burned):  # noqa: F811
    """A volume deprecated by consolidation, whose disc is ACTIVE, stays
    DEPRECATED when that disc is read."""
    conn = burned["conn"]
    vid = burned["volume_id"]
    create_location(conn, "Offsite")
    # Back to VERIFIED with a disc (as #726 would), then consolidated.
    _pre_726_disc(conn, vid, "Offsite")
    conn.execute("UPDATE volumes SET status = 'VERIFIED' WHERE volume_id = ?",
                 (vid,))
    conn.commit()
    update_status(conn, vid, "CONSOLIDATING")
    update_status(conn, vid, "DEPRECATED", force=True)

    assert _verify_disc(burned["config"].db_path, burned["label"]) == 0
    assert get_volume_by_id(conn, vid).status == "DEPRECATED"
