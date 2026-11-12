"""#728: one failed direct burn gives one next step in ``lcsas status``.

The burn command itself says to burn the session again
(``lcsas burn --session S --location L``) while the session's ISO is
still in the staging area.  ``status`` used to print that as a PARTIAL
session warning AND a different step (``stage --for-location``) under
"needs re-burn".  Now the needs-re-burn line names the same command
while the ISO is kept, the PARTIAL line is not printed for a session
already covered, and once the ISO is gone the step is to stage again.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import ensure_location
from lcsas.db.schema import create_all
from lcsas.db.sessions import add_session_volume, create_session
from lcsas.db.volumes import create_volume
from lcsas.iso.xorriso import MediaStatus

SESSION = "sess-728"
LABEL = "VOL_728"
LOCATION = "Bank_Box"
BURN_STEP = f"lcsas burn --session {SESSION} --location {LOCATION}"
RESTAGE_STEP = f"lcsas stage --for-location {LOCATION}"


@pytest.fixture
def staged(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Config + catalog with one staged volume whose ISO is on disk."""
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
    iso = staging / f"{LABEL}.iso"
    iso.write_bytes(b"\0" * 2048)
    conn = get_connection(db)
    try:
        create_all(conn)
        ensure_location(conn, LOCATION)
        vol = create_volume(conn, LABEL, "uuid-728", "TEST_TINY", 1000,
                            LOCATION, "STAGING")
        create_session(conn, "TEST_TINY", str(staging), session_id=SESSION)
        add_session_volume(
            conn, SESSION, vol.volume_id, str(iso),
            iso_sha256=hashlib.sha256(iso.read_bytes()).hexdigest(),
            iso_size_bytes=2048,
        )
        conn.commit()
    finally:
        conn.close()
    return cfg, db, iso


def _burn_fails_verify(cfg: Path, tmp_path: Path, monkeypatch, capsys) -> str:
    """Run `lcsas burn` with a drive whose read-back check fails."""
    import lcsas.iso.xorriso as xorriso_mod

    fake = MagicMock()
    fake.media_status = MagicMock(return_value=MediaStatus.BLANK)
    fake.verify_disc = MagicMock(return_value=False)
    monkeypatch.setattr(xorriso_mod, "SubprocessXorrisoRunner",
                        lambda *a, **k: fake)
    device = tmp_path / "sr0"
    device.write_bytes(b"")
    rc = main(["--config", str(cfg), "burn", "--session", SESSION,
               "--location", LOCATION, "--device", str(device),
               "--no-prompt"])
    assert rc == 1
    assert fake.burn_iso.called
    out = capsys.readouterr()
    return out.out + out.err


def _status(cfg: Path, capsys) -> str:
    assert main(["--config", str(cfg), "status"]) == 0
    out = capsys.readouterr()
    return out.out + out.err


def test_failed_direct_burn_gives_one_step(staged, tmp_path, monkeypatch, capsys):
    cfg, _db, _iso = staged
    burn_out = _burn_fails_verify(cfg, tmp_path, monkeypatch, capsys)
    assert "VERIFY FAILED" in burn_out
    assert BURN_STEP in burn_out

    out = _status(cfg, capsys)
    assert f"{LABEL} needs re-burn at {LOCATION}" in out
    # The same step as the burn command, once, and no other step.
    assert out.count(BURN_STEP) == 1
    assert "PARTIAL" not in out
    assert "stage --for-location" not in out
    assert "waiting to be burned" not in out


def test_step_is_restage_once_the_iso_is_gone(staged, tmp_path, monkeypatch,
                                              capsys):
    cfg, _db, iso = staged
    _burn_fails_verify(cfg, tmp_path, monkeypatch, capsys)
    iso.unlink()

    out = _status(cfg, capsys)
    assert f"{LABEL} needs re-burn at {LOCATION}" in out
    # One step: not also a ghost (reconcile) or a PARTIAL session line.
    assert out.count(RESTAGE_STEP) == 1
    assert "lcsas burn --session" not in out
    assert "catalog reconcile" not in out
    assert "PARTIAL" not in out


def test_partial_session_not_covered_still_warns(staged, capsys):
    """A PARTIAL session with no volume under "needs re-burn" (a burn
    stopped by an error, no verify result) keeps its own line."""
    cfg, db, _iso = staged
    conn = get_connection(db)
    try:
        conn.execute("UPDATE burn_sessions SET status = 'PARTIAL'")
        conn.commit()
    finally:
        conn.close()

    out = _status(cfg, capsys)
    assert f"session {SESSION} is PARTIAL" in out
    assert f"lcsas burn --session {SESSION} --location" in out
