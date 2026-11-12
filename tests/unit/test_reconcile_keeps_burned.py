"""``catalog reconcile --fix`` never deletes a burned volume (#746).

A STAGING/BURNING volume with no disc record is a ghost only when nothing
shows it was burned.  When its events show a good disc of it (the check
``session abort`` and ``stage --clean`` use since #737,
``lcsas.db.volume_events.burn_evidence``), its disc is just not recorded
yet: reconcile lists it, says how to record the disc, and ``--fix`` keeps
it, with or without ``--include-waiting``.  A real ghost beside it is
still deleted.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import add_session_volume, create_session
from lcsas.db.volume_events import add_event
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume, get_volume_by_label

EVIDENCE = {
    "verify_pass": ("VERIFY_PASS", "Home",
                    "Post-burn read-back + device hash match",
                    "a disc of it passed verification at Home"),
    "receipt": ("BURN_RECEIPT_IMPORTED", "Home",
                json.dumps({"verify_passed": True}),
                "a burn receipt of it was imported at Home"),
}


def _catalog(tmp_path: Path, evidence: str, *, isos: bool) -> Path:
    """GHOST (never burned) and BURNED_NO_DISC (an event shows a good
    disc, no disc record), both STAGING, 48 h old, one pack each.  With
    *isos* both staged ISOs still exist (waiting to be burned)."""
    db = tmp_path / "archive.db"
    conn = get_connection(db)
    try:
        create_all(conn)
        create_location(conn, "Home")
        register_repo(conn, "r1", "R1", "/mnt/r1")
        session_dir = tmp_path / "staging" / "sess"
        session_dir.mkdir(parents=True)
        create_session(conn, media_type="TEST_TINY",
                       staging_dir=str(session_dir), session_id="sess")
        for i, label in enumerate(("GHOST", "BURNED_NO_DISC")):
            pack = register_pack(conn, sha256=f"{i:x}" * 64, size_bytes=100,
                                 repo_id="r1")
            vol = create_volume(
                conn, label=label, uuid=f"uuid-{label}",
                media_type="TEST_TINY", capacity_bytes=2_097_152,
                status="STAGING",
            )
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
            conn.execute(
                "UPDATE volumes SET created_at = datetime('now', '-48 hours') "
                "WHERE volume_id = ?", (vol.volume_id,),
            )
            iso = session_dir / f"{label}.iso"
            if isos:
                iso.write_bytes(b"\x00" * 64)
            add_session_volume(conn, "sess", vol.volume_id, str(iso),
                               iso_sha256="c" * 64, iso_size_bytes=64)
            if label == "BURNED_NO_DISC":
                event_type, location, detail, _why = EVIDENCE[evidence]
                add_event(conn, vol.volume_id, event_type,
                          location=location, detail=detail)
        conn.commit()
    finally:
        conn.close()
    return db


@pytest.mark.parametrize("evidence", sorted(EVIDENCE))
@pytest.mark.parametrize("include_waiting", [False, True])
def test_fix_keeps_burned_volume_and_deletes_ghost(
    tmp_path, caplog, evidence, include_waiting,
):
    db = _catalog(tmp_path, evidence, isos=include_waiting)
    args = ["--db", str(db), "catalog", "reconcile", "--fix", "--yes"]
    if include_waiting:
        args.append("--include-waiting")
    caplog.clear()
    assert main(args) == 0, caplog.text
    out = caplog.text

    why = EVIDENCE[evidence][3]
    assert f"BURNED_NO_DISC  (status=STAGING, but {why})" in out
    assert "--fix never deletes them" in out
    assert "--disc --location <where it is kept>" in out
    assert "Deleting ghost volume GHOST" in out
    assert "Deleting ghost volume BURNED_NO_DISC" not in out

    conn = get_connection(db)
    try:
        assert get_volume_by_label(conn, "GHOST") is None
        kept = get_volume_by_label(conn, "BURNED_NO_DISC")
        assert kept is not None and kept.status == "STAGING"
        # Only the ghost's pack went back to the pool.
        assert {p.sha256 for p in get_unarchived_packs(conn)} == {
            "0" * 64, "1" * 64,
        }
        assert conn.execute(
            "SELECT COUNT(*) FROM volume_packs WHERE volume_id = ?",
            (kept.volume_id,),
        ).fetchone()[0] == 1
    finally:
        conn.close()


def test_report_lists_burned_volume_apart_from_ghosts(tmp_path, caplog):
    db = _catalog(tmp_path, "verify_pass", isos=False)
    caplog.clear()
    assert main(["--db", str(db), "catalog", "reconcile"]) == 1
    out = caplog.text
    assert "1 ghost volume(s)" in out
    assert "BURNED_NO_DISC  (status=STAGING, but a disc of it passed" in out
