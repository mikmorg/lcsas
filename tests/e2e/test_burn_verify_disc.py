"""CDEmu e2e for device read-back verification [BURN-04].

Proves the load-bearing property end to end against a real (virtual)
optical device: ``lcsas verify --disc`` passes for a disc whose bytes
match the ISO SHA-256 recorded in the catalog, and FAILS when a single
byte of the backing image differs — the case ``xorriso -check_media``
alone can never catch (a bit-flipped disc is still perfectly readable).

Flow:
  1. Master a small ISO with xorriso and register it in a fresh catalog
     (volume row + session_volumes row carrying iso_sha256 +
     iso_size_bytes, exactly what ``lcsas stage`` records).
  2. Load the ISO into cdemu device 0 and run
     ``lcsas verify <label> --disc --device /dev/srX`` → expect PASS and
     BURNED → VERIFIED promotion + last_verified_at stamped.
  3. Unload, flip ONE byte in the backing file, reload, re-run → expect
     FAIL (exit 1) with a VERIFY_FAIL event recorded.

SLOW + opt-in (pattern of ``LCSAS_ECC_REPAIR=1``): gated behind
``LCSAS_BURN_E2E=1``, never runs in the default suite::

    LCSAS_BURN_E2E=1 pytest tests/e2e/test_burn_verify_disc.py -v
    # or: make verify-burn-e2e

Requires the cdemu daemon (session bus) and xorriso; commandeers cdemu
device 0 and restores whatever image was loaded there afterwards.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_xorriso,
    pytest.mark.requires_cdemu,
    pytest.mark.skipif(
        not os.environ.get("LCSAS_BURN_E2E"),
        reason="set LCSAS_BURN_E2E=1 to run the cdemu burn-verify drill "
        "(commandeers cdemu device 0; ~30s)",
    ),
]


def _cdemu(*args: str) -> str:
    """Run the cdemu client; fail loud (the test was explicitly opted into)."""
    result = subprocess.run(
        ["cdemu", *args], capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"cdemu {' '.join(args)} failed (rc={result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout


def _device_node() -> str:
    """Return the SCSI CD-ROM node for cdemu device 0 (e.g. /dev/sr0)."""
    for line in _cdemu("device-mapping").splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "0" and fields[1].startswith("/dev/"):
            return fields[1]
    raise RuntimeError("cdemu device 0 not found in device-mapping output")


def _loaded_image() -> str | None:
    """Return the image currently loaded in cdemu device 0, if any."""
    for line in _cdemu("status").splitlines():
        fields = line.split(None, 2)
        if len(fields) >= 2 and fields[0] == "0":
            return fields[2] if fields[1] == "True" and len(fields) > 2 else None
    return None


def _load(iso: Path, device: str) -> None:
    _cdemu("load", "0", str(iso))
    # Wait for the kernel to see the new medium.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            with open(device, "rb") as fh:
                if fh.read(2048):
                    return
        except OSError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"medium never became readable on {device}")


def _unload() -> None:
    _cdemu("unload", "0")
    time.sleep(1)


@pytest.fixture
def cdemu_device():
    """Commandeer cdemu device 0, restoring its previous image afterwards."""
    if shutil.which("cdemu") is None or shutil.which("xorriso") is None:
        pytest.fail("LCSAS_BURN_E2E=1 set but cdemu/xorriso not installed")
    previous = _loaded_image()
    if previous:
        _unload()
    try:
        yield _device_node()
    finally:
        with contextlib.suppress(RuntimeError):
            _unload()
        if previous and Path(previous).exists():
            _cdemu("load", "0", previous)


def test_verify_disc_device_hash_pass_then_byte_flip_fails(
    cdemu_device, tmp_path,
):
    from lcsas.cli.main import main
    from lcsas.db.connection import get_connection
    from lcsas.db.schema import create_all
    from lcsas.db.sessions import add_session_volume, create_session
    from lcsas.db.volume_copies import get_copies_for_volume
    from lcsas.db.volume_events import get_events_for_volume
    from lcsas.db.volumes import create_volume, get_volume_by_label, update_status
    from lcsas.utils.hashing import sha256_file

    label = "LCSAS-E2E-BURN04"
    location = "E2E_Shelf"

    # 1. Master a small ISO.
    content_dir = tmp_path / "content"
    content_dir.mkdir()
    (content_dir / "payload.bin").write_bytes(os.urandom(256 * 1024))
    iso_path = tmp_path / f"{label}.iso"
    subprocess.run(
        ["xorriso", "-as", "mkisofs", "-o", str(iso_path), "-V", label,
         "-r", str(content_dir)],
        check=True, capture_output=True, timeout=120,
    )
    iso_hash = sha256_file(iso_path)
    iso_size = iso_path.stat().st_size

    # 2. Fresh catalog with the volume registered the way stage() +
    #    burn_session() record it (BURNED, hash + size on the session row).
    db_path = tmp_path / "archive.db"
    conn = get_connection(db_path)
    create_all(conn)
    vol = create_volume(
        conn, label=label, uuid="e2e-burn04-uuid", media_type="TEST_TINY",
        capacity_bytes=2_097_152,
    )
    update_status(conn, vol.volume_id, "BURNING")
    update_status(conn, vol.volume_id, "BURNED")
    session = create_session(
        conn, media_type="TEST_TINY", staging_dir=str(tmp_path),
    )
    add_session_volume(
        conn, session_id=session.session_id, volume_id=vol.volume_id,
        iso_path=str(iso_path), iso_sha256=iso_hash, iso_size_bytes=iso_size,
    )
    conn.execute(
        "INSERT INTO locations (name) VALUES (?)", (location,),
    )
    conn.execute(
        "INSERT INTO volume_copies (volume_id, location, burn_date) "
        "VALUES (?, ?, '2026-01-01T00:00:00+00:00')",
        (vol.volume_id, location),
    )
    conn.commit()
    conn.close()

    # 3. Pristine disc → verify passes, BURNED → VERIFIED, copy stamped.
    _load(iso_path, cdemu_device)
    rc = main([
        "--db", str(db_path),
        "verify", label, "--disc", "--device", cdemu_device,
        "--location", location,
    ])
    assert rc == 0, "pristine disc must pass the device hash verify"

    conn = get_connection(db_path)
    vol_after = get_volume_by_label(conn, label)
    assert vol_after.status == "VERIFIED"
    copies = get_copies_for_volume(conn, vol_after.volume_id)
    assert copies and copies[0].last_verified_at is not None
    conn.close()

    # 4. Flip ONE byte in the backing file — the disc stays perfectly
    #    readable (check_media alone would still pass), but the hash
    #    compare must fail.
    _unload()
    flipped = bytearray(iso_path.read_bytes())
    flipped[len(flipped) // 2] ^= 0xFF
    iso_path.write_bytes(bytes(flipped))
    _load(iso_path, cdemu_device)

    rc = main([
        "--db", str(db_path),
        "verify", label, "--disc", "--device", cdemu_device,
    ])
    assert rc == 1, "a byte-flipped disc must fail the device hash verify"

    conn = get_connection(db_path)
    events = get_events_for_volume(
        conn, vol_after.volume_id, "VERIFY_FAIL",
    )
    assert events, "the failed verify must leave a VERIFY_FAIL audit event"
    conn.close()
