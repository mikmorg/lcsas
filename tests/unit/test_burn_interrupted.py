"""#668: an interrupted ``burn --session`` does not leave a stuck volume.

Ctrl-C during the burn returns the volume to STAGING.  A volume a hard
kill left in BURNING (no disc recorded) is burned by the next run, and
the status check runs before the prompt for a blank disc.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.db.connection import get_memory_connection
from lcsas.db.locations import ensure_location
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy, get_copies_for_volume
from lcsas.db.volumes import get_volume_by_id, update_status
from lcsas.exceptions import BurnError
from tests.unit.test_session_pipeline import (
    _make_config,
    _seed_packs,
    _wire_drive_identity,
    _zeros_device_reader,
)


class _RecordingPrompt:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def checkpoint(self, message: str) -> None:
        self.messages.append(message)


@pytest.fixture
def staged(tmp_path, monkeypatch):
    monkeypatch.setattr("lcsas.burn.orchestrator.eject_tray", lambda _d: True)
    config = _make_config(tmp_path, num_repos=1)
    conn = get_memory_connection()
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=3, pack_size=50)

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    prompt = _RecordingPrompt()
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_zeros_device_reader,
                            prompt=prompt)
    result = orch.stage()
    [manifest] = result.manifests
    return {
        "orch": orch, "conn": conn, "xorriso": xorriso, "prompt": prompt,
        "session_id": result.session_id, "volume_id": manifest.volume_id,
    }


def _burn(s):
    return s["orch"].burn_session(s["session_id"], location="Home_Shelf",
                                  device="/dev/sr0", interactive=True)


def test_ctrl_c_during_burn_leaves_staging(staged):
    staged["xorriso"].burn_iso.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        _burn(staged)
    vol = get_volume_by_id(staged["conn"], staged["volume_id"])
    assert vol.status == "STAGING"

    # And the next run burns it.
    staged["xorriso"].burn_iso.side_effect = None
    [receipt] = _burn(staged)
    assert receipt.verify_passed
    assert get_volume_by_id(staged["conn"], staged["volume_id"]).status == "VERIFIED"


def test_burning_with_no_copies_is_burned_by_next_run(staged, caplog):
    conn = staged["conn"]
    update_status(conn, staged["volume_id"], "BURNING")  # a hard kill

    with caplog.at_level(logging.WARNING, logger="lcsas"):
        [receipt] = _burn(staged)

    assert receipt.verify_passed
    assert get_volume_by_id(conn, staged["volume_id"]).status == "VERIFIED"
    assert len(get_copies_for_volume(conn, staged["volume_id"])) == 1
    assert "interrupted burn" in caplog.text


def test_status_check_runs_before_blank_disc_prompt(staged):
    conn = staged["conn"]
    update_status(conn, staged["volume_id"], "BURNING")
    ensure_location(conn, "Home_Shelf")
    add_volume_copy(conn, staged["volume_id"], "Home_Shelf")

    with pytest.raises(BurnError, match="already has a disc"):
        _burn(staged)
    assert staged["prompt"].messages == []
    staged["xorriso"].media_status.assert_not_called()
    staged["xorriso"].burn_iso.assert_not_called()
