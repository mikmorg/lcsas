"""#665: a stage killed during ECC leaves no finished-looking ISO.

The ISO is mastered and augmented under a working name and gets its
final name ``<label>.iso`` only after ECC, the size check and the hash
record.  A volume whose session row has no hash is "staging incomplete":
``burn --session`` refuses it before asking for a disc, and ``lcsas
status`` reports it as incomplete, not as waiting.
"""

from __future__ import annotations

import hashlib
import logging
import multiprocessing
import os
from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator, unfinished_iso_path
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.db.connection import get_connection
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import (
    get_latest_session,
    get_session_volumes,
    staging_incomplete,
)
from lcsas.db.volumes import list_volumes
from lcsas.exceptions import BurnError
from tests.unit.test_session_pipeline import (
    _make_config,
    _seed_packs,
    _wire_drive_identity,
    _zeros_device_reader,
)

_Usage = namedtuple("_Usage", "total used free")


class _RecordingPrompt:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def checkpoint(self, message: str) -> None:
        self.messages.append(message)


def _fake_xorriso() -> MagicMock:
    xorriso = MagicMock()

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    _wire_drive_identity(xorriso)
    return xorriso


def _setup(tmp_path: Path):
    """A file-backed catalog with a few packs, staged for CD700 (ECC media)."""
    config = _make_config(tmp_path, num_repos=1, media=MediaType.CD700)
    conn = get_connection(config.db_path)
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=3, pack_size=50)
    conn.commit()
    return config, conn


def _patch_disk_space(monkeypatch) -> None:
    # CD700 budgets a full 737 MB padded image per volume; the fakes
    # write 1 KB, so do not let the free-space check need the real room.
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )


def _stage_and_die_during_ecc(config) -> None:
    """Child process: stage, and die (no cleanup at all) inside ECC."""
    conn = get_connection(config.db_path)
    dvdisaster = MagicMock()

    def _killed(iso_path, *_a, **_kw):
        os._exit(137)  # like SIGKILL / power loss: no handler runs

    dvdisaster.augment_iso.side_effect = _killed
    orch = BurnOrchestrator(config, conn, _fake_xorriso(), dvdisaster,
                            prompt=NullOperatorPrompt())
    orch.stage()
    os._exit(0)  # pragma: no cover - never reached


class TestKilledDuringEcc:
    def test_kill_during_ecc_leaves_incomplete_volume(
        self, tmp_path, monkeypatch, caplog,
    ):
        config, conn = _setup(tmp_path)
        conn.close()
        _patch_disk_space(monkeypatch)

        child = multiprocessing.get_context("fork").Process(
            target=_stage_and_die_during_ecc, args=(config,),
        )
        child.start()
        child.join(60)
        assert child.exitcode == 137

        # 1. No file named <label>.iso in the staging folder.
        conn = get_connection(config.db_path)
        [vol] = list_volumes(conn)
        assert vol.status == "STAGING"
        staged = [p.name for p in config.staging_path.rglob("*") if p.is_file()]
        assert f"{vol.label}.iso" not in staged
        assert not [n for n in staged if n.endswith(".iso")]
        assert f"{vol.label}.unfinished" in staged

        # The catalog knows the staging never finished.
        session = get_latest_session(conn)
        [sv] = get_session_volumes(conn, session.session_id)
        assert sv.iso_sha256 == ""
        assert staging_incomplete(conn, vol.volume_id)

        # 2. burn --session refuses it, before any prompt or disc check.
        prompt = _RecordingPrompt()
        xorriso = _fake_xorriso()
        orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                                device_reader=_zeros_device_reader,
                                prompt=prompt)
        with pytest.raises(BurnError) as exc:
            orch.burn_session(session.session_id, location="Home_Shelf",
                              device="/dev/sr0")
        assert vol.label in str(exc.value)
        assert "session abort" in exc.value.recovery_hint
        assert prompt.messages == []
        xorriso.media_status.assert_not_called()
        xorriso.burn_iso.assert_not_called()
        assert list_volumes(conn)[0].status == "STAGING"
        conn.close()

        # 3. lcsas status shows it as incomplete, not as waiting.
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="lcsas"):
            assert main(["--db", str(config.db_path), "status"]) == 0
        text = caplog.text
        assert "INCOMPLETE" in text
        assert vol.label in text
        assert session.session_id in text
        assert "waiting to be burned" not in text

    def test_waiting_iso_with_empty_hash_is_incomplete_too(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Even a <label>.iso that exists is not burnable without its hash."""
        config, conn = _setup(tmp_path)
        _patch_disk_space(monkeypatch)
        orch = BurnOrchestrator(config, conn, _fake_xorriso(), MagicMock(),
                                prompt=NullOperatorPrompt())
        result = orch.stage()
        [manifest] = result.manifests
        conn.execute("UPDATE session_volumes SET iso_sha256 = ''")
        conn.commit()
        assert manifest.iso_path is not None and manifest.iso_path.exists()
        assert staging_incomplete(conn, manifest.volume_id)
        with pytest.raises(BurnError):
            orch.burn_session(result.session_id, location="Home_Shelf",
                              device="/dev/sr0", skip_burn=True)
        conn.close()

        caplog.clear()
        with caplog.at_level(logging.INFO, logger="lcsas"):
            assert main(["--db", str(config.db_path), "status"]) == 0
        assert "INCOMPLETE" in caplog.text
        assert "waiting to be burned" not in caplog.text


class TestFinishedStage:
    def test_ecc_runs_on_working_name_and_final_iso_is_hashed(
        self, tmp_path, monkeypatch,
    ):
        config, conn = _setup(tmp_path)
        _patch_disk_space(monkeypatch)
        dvdisaster = MagicMock()
        seen: list[Path] = []

        def _augment(iso_path, *_a, **_kw):
            seen.append(Path(iso_path))
            with open(iso_path, "ab") as f:
                f.write(b"\x01" * 256)  # "RS03 data"

        dvdisaster.augment_iso.side_effect = _augment
        orch = BurnOrchestrator(config, conn, _fake_xorriso(), dvdisaster,
                                prompt=NullOperatorPrompt())
        result = orch.stage()
        [manifest] = result.manifests
        final = manifest.iso_path
        assert final is not None and final.name == f"{manifest.volume_label}.iso"
        assert seen == [unfinished_iso_path(final)]
        assert final.exists()
        assert not unfinished_iso_path(final).exists()

        [sv] = get_session_volumes(conn, result.session_id)
        digest = hashlib.sha256(final.read_bytes()).hexdigest()
        assert sv.iso_sha256 == digest == manifest.iso_sha256
        assert sv.iso_size_bytes == final.stat().st_size == 1024 + 256
        assert sv.iso_path == str(final)
        assert not staging_incomplete(conn, manifest.volume_id)

        # A finished volume burns normally.
        orch.burn_session(result.session_id, location="Home_Shelf",
                          device="/dev/sr0", skip_burn=True)
        assert list_volumes(conn)[0].status == "VERIFIED"
        conn.close()

    def test_interrupt_during_ecc_cleans_up(self, tmp_path, monkeypatch):
        """Ctrl-C during ECC: the volume is rolled back, the work file removed."""
        config, conn = _setup(tmp_path)
        _patch_disk_space(monkeypatch)
        dvdisaster = MagicMock()
        dvdisaster.augment_iso.side_effect = KeyboardInterrupt
        orch = BurnOrchestrator(config, conn, _fake_xorriso(), dvdisaster,
                                prompt=NullOperatorPrompt())
        with pytest.raises(KeyboardInterrupt):
            orch.stage()
        assert list_volumes(conn) == []
        leftovers = [p.name for p in config.staging_path.rglob("*")
                     if p.is_file() and p.suffix in (".iso", ".unfinished")]
        assert leftovers == []
        conn.close()


def test_unfinished_name_is_not_an_iso():
    p = Path("/staging/s1/LCSAS_BD25_2026_0001.iso")
    assert unfinished_iso_path(p).name == "LCSAS_BD25_2026_0001.unfinished"
