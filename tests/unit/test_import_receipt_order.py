"""#754: an older receipt never overwrites a newer disc record.

A volume burned twice at the same location has two receipts.  A catalog
that did not write either burn (rebuilt from discs) imports them in any
order; the disc record must end up describing the newer disc: its burn
date, verify time and ISO hash.

The burns are the real orchestrator with xorriso and the drive faked
(``test_receipt_replay_direct_burn``).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from tests.unit.test_receipt_replay_direct_burn import (
    _burn,
    _catalog_state,
    env,  # noqa: F401  (fixture)
)


def _two_receipts(env) -> tuple[Path, Path]:  # noqa: F811
    first = _burn(env, skip_burn=False)
    older = first.parent.parent / "older.json"
    shutil.move(first, older)
    second = _burn(env, skip_burn=False)
    newer = second.parent.parent / "newer.json"
    shutil.move(second, newer)
    return older, newer


def _rebuilt(env, tmp_path: Path) -> Path:  # noqa: F811
    """A catalog rebuilt from the volume's own disc: no disc record."""
    rebuilt = tmp_path / "rebuilt" / "archive.db"
    rebuilt.parent.mkdir()
    assert main(["catalog", "rebuild", str(env["tree"]),
                 "--output", str(rebuilt)]) == 0
    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(env["cfg"].read_text().replace(
        str(env["config"].db_path), str(rebuilt)))
    return cfg


def _import(cfg: Path, receipt: Path, caplog) -> str:
    caplog.clear()
    # The rebuilt catalog has no staged ISO hash: --allow-unhashed.
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 "--allow-unhashed", str(receipt)]) == 0, caplog.text
    return caplog.text


def _disc(db: Path, label: str) -> tuple:
    conn = get_connection(db)
    try:
        row = conn.execute(
            "SELECT c.status, c.burn_date, c.last_verified_at, c.iso_sha256, "
            "c.iso_size_bytes, v.status FROM volume_copies c "
            "JOIN volumes v USING (volume_id) WHERE v.label = ?", (label,),
        ).fetchone()
        return tuple(row)
    finally:
        conn.close()


def _db(cfg: Path) -> Path:
    return cfg.parent / "rebuilt" / "archive.db"


def _expected(receipt: Path) -> tuple:
    r = json.loads(receipt.read_text())
    return ("ACTIVE", r["burn_date"], r["burn_date"], r["iso_sha256"],
            r["iso_size_bytes"], "VERIFIED")


def test_newer_then_older_keeps_the_newer_disc(env, caplog, tmp_path):  # noqa: F811
    older, newer = _two_receipts(env)
    assert (json.loads(older.read_text())["burn_date"]
            < json.loads(newer.read_text())["burn_date"])
    cfg = _rebuilt(env, tmp_path)

    _import(cfg, newer, caplog)
    assert _disc(_db(cfg), env["label"]) == _expected(newer)

    out = _import(cfg, older, caplog)
    assert "The newer disc record is kept" in out
    assert _disc(_db(cfg), env["label"]) == _expected(newer)

    # A re-run of either changes nothing.
    before = _catalog_state(_db(cfg))
    for receipt in (older, newer):
        out = _import(cfg, receipt, caplog)
        assert "already imported" in out
    assert _catalog_state(_db(cfg)) == before


def test_older_then_newer_takes_the_newer_disc(env, caplog, tmp_path):  # noqa: F811
    older, newer = _two_receipts(env)
    cfg = _rebuilt(env, tmp_path)

    _import(cfg, older, caplog)
    assert _disc(_db(cfg), env["label"]) == _expected(older)
    _import(cfg, newer, caplog)
    assert _disc(_db(cfg), env["label"]) == _expected(newer)

    before = _catalog_state(_db(cfg))
    for receipt in (older, newer):
        assert "already imported" in _import(cfg, receipt, caplog)
    assert _catalog_state(_db(cfg)) == before


def test_older_failed_receipt_does_not_ask_for_a_reburn(
    env, caplog, tmp_path,  # noqa: F811
):
    """A failed older disc, imported after the newer good one, writes no
    VERIFY_FAIL_REBURN, so status does not ask for a re-burn."""
    first = _burn(env, skip_burn=False, disc_ok=False)
    older = first.parent.parent / "older.json"
    shutil.move(first, older)
    assert json.loads(older.read_text())["verify_passed"] is False
    second = _burn(env, skip_burn=False)
    cfg = _rebuilt(env, tmp_path)
    _import(cfg, second, caplog)

    out = _import(cfg, older, caplog)
    assert "The newer disc record is kept" in out
    conn = get_connection(_db(cfg))
    try:
        events = [r[0] for r in conn.execute(
            "SELECT event_type FROM volume_events ORDER BY event_id")]
    finally:
        conn.close()
    assert "VERIFY_FAIL_REBURN" not in events
    assert "VERIFY_FAIL" not in events
    caplog.clear()
    assert main(["--config", str(cfg), "status"]) == 0
    assert "needs re-burn" not in caplog.text


def test_receipt_fills_the_hash_of_a_disc_recorded_by_verify(
    env, caplog, tmp_path,  # noqa: F811
):
    """After a rebuild, 'verify --disc --location' records the disc dated
    when it was recorded, with no ISO hash.  Importing the disc's (older)
    receipt then keeps that record's dates but adds the hash."""
    receipt = _burn(env, skip_burn=False)
    cfg = _rebuilt(env, tmp_path)
    db = _db(cfg)
    label = env["label"]
    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value=label),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
        patch("lcsas.cli.main._verify_disc_against_recorded_hash",
              return_value=True),
    ):
        assert main(["--db", str(db), "verify", label, "--disc",
                     "--device", "/dev/null", "--location",
                     "Home_Shelf"]) == 0, caplog.text
    status, burn_date, verified_at, iso_hash, size, vstatus = _disc(db, label)
    assert iso_hash is None and vstatus == "VERIFIED"

    out = _import(cfg, receipt, caplog)
    assert "ISO hash and size were added" in out
    r = json.loads(receipt.read_text())
    assert _disc(db, label) == (
        "ACTIVE", burn_date, verified_at, r["iso_sha256"],
        r["iso_size_bytes"], "VERIFIED",
    )
    before = _catalog_state(db)
    assert "already imported" in _import(cfg, receipt, caplog)
    assert _catalog_state(db) == before
