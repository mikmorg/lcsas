"""#674: thin RS03 redundancy fails ``lcsas stage`` unless overridden.

RS03 pads an ISO to the smallest fitting medium; the padding is the
parity.  Stage refuses a volume whose redundancy would be below
MIN_EFFECTIVE_REDUNDANCY_PCT, and refuses before mastering when the
content (packs + disc metadata) exceeds the medium's usable bytes.
``--allow-low-redundancy`` proceeds and records the percentage.
"""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator, LowRedundancyError
from lcsas.config.media import MediaType
from lcsas.db.connection import get_memory_connection
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import list_volumes
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")
CD = 737_280_000


def _env(tmp_path, monkeypatch, *, media, iso_bytes=1024, allow=False):
    monkeypatch.setattr(
        "lcsas.burn.orchestrator.shutil.disk_usage",
        lambda _p: _Usage(10**13, 0, 10**13),
    )
    config = _make_config(tmp_path, num_repos=1, media=media)
    conn = get_memory_connection()
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=2, pack_size=50)

    xorriso = MagicMock()

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        with open(output_iso, "wb") as f:
            f.truncate(iso_bytes)  # sparse: no real disk use
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    dvdisaster = MagicMock()
    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster,
                            prompt=NullOperatorPrompt(),
                            allow_low_redundancy=allow)
    return orch, conn, xorriso, dvdisaster, config


def test_thin_redundancy_fails_stage_before_ecc(tmp_path, monkeypatch):
    # 2% below the CD rung: ~2% parity.
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700,
        iso_bytes=int(CD / 1.02),
    )
    with pytest.raises(LowRedundancyError) as exc:
        orch.stage()
    msg = str(exc.value)
    assert "_0001" in msg  # names the volume
    assert "2.0%" in msg
    assert "--allow-low-redundancy" in exc.value.recovery_hint
    dvdisaster.augment_iso.assert_not_called()
    assert list_volumes(conn) == []
    assert len(get_unarchived_packs(conn)) == 2


def test_override_records_percentage_in_volume_event(tmp_path, monkeypatch):
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700,
        iso_bytes=int(CD / 1.02), allow=True,
    )
    result = orch.stage()
    dvdisaster.augment_iso.assert_called_once()
    [m] = result.manifests
    notes = [e.detail for e in get_events_for_volume(conn, m.volume_id)
             if e.event_type == "NOTE"]
    assert any("2.0%" in n and "--allow-low-redundancy" in n for n in notes)


def test_ample_redundancy_stages_normally(tmp_path, monkeypatch):
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700, iso_bytes=CD // 2,
    )
    result = orch.stage()
    assert len(result.manifests) == 1
    dvdisaster.augment_iso.assert_called_once()
    [m] = result.manifests
    assert not [e for e in get_events_for_volume(conn, m.volume_id)
                if e.event_type == "NOTE"]


def test_oversized_metadata_fails_before_mastering(tmp_path, monkeypatch):
    """Disc metadata that outgrew the reserve: content > usable bytes."""
    orch, conn, xorriso, _d, config = _env(
        tmp_path, monkeypatch, media=MediaType.TEST_TINY,
    )
    # A repository index far larger than the 100-byte metadata reserve.
    index = config.repositories["family"].mirror_path / "index"
    (index / "big").write_bytes(b"\x01" * (MediaType.TEST_TINY.usable_bytes + 1))

    with pytest.raises(LowRedundancyError, match="usable bytes"):
        orch.stage()
    xorriso.create_iso.assert_not_called()
    assert list_volumes(conn) == []
