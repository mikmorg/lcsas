"""#672: a second plain ``lcsas stage`` does not restage waiting packs.

Packs on a never-burned volume whose finished ISO waits in the staging
area are on their way to a disc.  Staging them again would master a
second disc of the same data.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator, NothingToStageError
from lcsas.db.connection import get_memory_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_session_volumes
from lcsas.db.volume_events import add_event
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import list_volumes
from lcsas.exceptions import StagingError
from tests.unit.test_session_pipeline import (
    _make_config,
    _pack_content,
    _seed_packs,
    _wire_drive_identity,
    _zeros_device_reader,
)


def test_stage_twice_makes_no_second_volume(env):
    orch, conn = env["orch"], env["conn"]
    first = orch.stage()
    assert len(first.manifests) == 1

    with pytest.raises(NothingToStageError) as exc:
        orch.stage()
    assert first.session_id in str(exc.value)
    assert first.manifests[0].volume_label in str(exc.value)
    assert "session abort" in exc.value.recovery_hint
    # Still a ValueError for callers that caught "No packs need staging".
    assert isinstance(exc.value, ValueError)
    assert isinstance(exc.value, StagingError)
    assert len(list_volumes(conn)) == 1


def test_second_stage_takes_only_new_packs(env, caplog):
    orch, conn, config = env["orch"], env["conn"], env["config"]
    first = orch.stage()
    first_ids = set(get_pack_ids_for_volume(conn, first.manifests[0].volume_id))

    # A new pack arrives after the first stage.
    sha, content = _pack_content(99, 50)
    new = register_pack(conn, sha256=sha, size_bytes=50, repo_id="family")
    (config.repositories["family"].mirror_path / "data" / sha).write_bytes(content)

    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="lcsas"):
        second = orch.stage()
    [m] = second.manifests
    assert set(get_pack_ids_for_volume(conn, m.volume_id)) == {new.pack_id}
    assert not first_ids & {new.pack_id}
    assert first.session_id in caplog.text


def test_aborted_session_packs_are_staged_again(env):
    orch = env["orch"]
    first = orch.stage()
    orch.abort_session(first.session_id)
    second = orch.stage()
    assert len(second.manifests) == 1


def test_iso_gone_releases_packs(env):
    """A volume whose ISO is gone is a ghost, not waiting: as before."""
    orch = env["orch"]
    first = orch.stage()
    first.manifests[0].iso_path.unlink()
    second = orch.stage()
    assert len(second.manifests) == 1


def test_verify_failed_volume_does_not_hold_packs(env):
    """As for --for-location (#642/#483): a failed disc releases its packs."""
    orch, conn = env["orch"], env["conn"]
    first = orch.stage()
    add_event(conn, first.manifests[0].volume_id, "VERIFY_FAIL",
              detail="test")
    second = orch.stage()
    assert len(second.manifests) == 1


def test_burned_session_then_stage_is_still_nothing(env):
    orch, conn = env["orch"], env["conn"]
    first = orch.stage()
    orch.burn_session(first.session_id, location="Home_Shelf",
                      device="/dev/sr0", skip_burn=True)
    assert get_session_volumes(conn, first.session_id)
    with pytest.raises(ValueError, match="No packs need staging"):
        orch.stage()


@pytest.fixture
def env(tmp_path):
    config = _make_config(tmp_path, num_repos=2)
    conn = get_memory_connection()
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    packs = _seed_packs(conn, config, num_packs=5, pack_size=50)

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_zeros_device_reader,
                            prompt=NullOperatorPrompt())
    return {"orch": orch, "config": config, "conn": conn, "packs": packs}
