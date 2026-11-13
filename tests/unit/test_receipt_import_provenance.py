"""#734: BURN_RECEIPT_IMPORTED records how the disc was checked.

`lcsas burn-iso` receipts carry ``device_sha256`` (the hash read back
from the disc, #664) and ``verify_only`` (#673).  The import keeps both in
the event detail, so the catalog alone shows how a disc was verified.
Session-burn receipts carry neither and still import, without the keys.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import ensure_location
from lcsas.db.schema import create_all
from lcsas.db.sessions import add_session_volume, create_session
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import create_volume, get_volume_by_label

ISO_SHA = "a" * 64
DEVICE_SHA = ISO_SHA


@pytest.fixture
def env(tmp_path: Path) -> tuple[Path, Path]:
    staging = tmp_path / "staging"
    staging.mkdir()
    mirror = tmp_path / "mirror"
    mirror.mkdir()
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(f"""
[paths]
mirror_base = "{mirror}"
staging = "{staging}"
database = "{db}"

[defaults]
media_type = "TEST_TINY"
metadata_reserve_mb = 0
""")
    conn = get_connection(db)
    try:
        create_all(conn)
        ensure_location(conn, "Offsite")
        for i, label in enumerate(("VOL_A", "VOL_B")):
            vol = create_volume(conn, label, f"uuid-{i}", "TEST_TINY", 1000,
                                "Offsite", "STAGING")
            sid = f"sess-{i}"
            create_session(conn, "TEST_TINY", str(staging), session_id=sid)
            add_session_volume(conn, sid, vol.volume_id,
                               str(staging / f"{label}.iso"),
                               iso_sha256=ISO_SHA, iso_size_bytes=2048)
        conn.commit()
    finally:
        conn.close()
    return cfg, db


def _receipt(tmp_path: Path, label: str, **extra: object) -> Path:
    body = {
        "volume_label": label,
        "location": "Offsite",
        "session_id": "s1",
        "device": "/dev/sr0",
        "burn_date": "2026-04-14T12:00:00+00:00",
        "iso_sha256": ISO_SHA,
        "iso_size_bytes": 2048,
        "verify_passed": True,
        **extra,
    }
    p = tmp_path / f"{label}.json"
    p.write_text(json.dumps(body))
    return p


def _detail(db: Path, label: str) -> dict:
    conn = get_connection(db)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol is not None
        events = get_events_for_volume(conn, vol.volume_id,
                                       "BURN_RECEIPT_IMPORTED")
        assert len(events) == 1
        return json.loads(events[0].detail or "{}")
    finally:
        conn.close()


def test_burn_iso_receipt_fields_are_kept(env, tmp_path):
    cfg, db = env
    receipt = _receipt(tmp_path, "VOL_A", device_sha256=DEVICE_SHA,
                       verify_only=True)
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 str(receipt)]) == 0
    detail = _detail(db, "VOL_A")
    assert detail["device_sha256"] == DEVICE_SHA
    assert detail["verify_only"] is True


def test_null_device_hash_is_kept_as_null(env, tmp_path):
    """--no-verify burn-iso: nothing was read back."""
    cfg, db = env
    receipt = _receipt(tmp_path, "VOL_A", device_sha256=None,
                       verify_only=False, verify_passed=None)
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 str(receipt)]) == 0
    detail = _detail(db, "VOL_A")
    assert detail["device_sha256"] is None
    assert detail["verify_only"] is False


def test_session_receipt_without_fields_still_imports(env, tmp_path):
    cfg, db = env
    receipt = _receipt(tmp_path, "VOL_B", pack_ids=[], pack_count=0)
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 str(receipt)]) == 0
    detail = _detail(db, "VOL_B")
    assert "device_sha256" not in detail
    assert "verify_only" not in detail
    assert detail["iso_sha256"] == ISO_SHA
