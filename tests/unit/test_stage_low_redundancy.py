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
from lcsas.iso.xorriso import XORRISO_DEFAULT_PADDING_BYTES
from tests.unit.test_session_pipeline import _make_config, _seed_packs

_Usage = namedtuple("_Usage", "total used free")
# The real RS03 image of the CD rung (255 x 1409 sectors).  The ISO
# sizes below stay on that rung: past 246 x 1409 sectors dvdisaster
# moves up to DVD (#732), so "2% parity on a CD" cannot happen.
CD_AUG = 735_836_160
THIN_CD_ISO = int(CD_AUG / 1.04)        # ~4.0% parity


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
        size = iso_bytes
        if kwargs.get("padding_bytes") is not None:
            # xorriso -padding replaces the default 300 KiB pad.
            size += kwargs["padding_bytes"] - XORRISO_DEFAULT_PADDING_BYTES
        with open(output_iso, "wb") as f:
            f.truncate(size)  # sparse: no real disk use
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    dvdisaster = MagicMock()
    orch = BurnOrchestrator(config, conn, xorriso, dvdisaster,
                            prompt=NullOperatorPrompt(),
                            allow_low_redundancy=allow)
    return orch, conn, xorriso, dvdisaster, config


def test_thin_redundancy_fails_stage_before_ecc(tmp_path, monkeypatch):
    # ~4% parity on the CD rung of a CD: no higher rung fits the disc.
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700,
        iso_bytes=THIN_CD_ISO,
    )
    with pytest.raises(LowRedundancyError) as exc:
        orch.stage()
    msg = str(exc.value)
    assert "_0001" in msg  # names the volume
    assert "4.0%" in msg
    assert "--allow-low-redundancy" in exc.value.recovery_hint
    dvdisaster.augment_iso.assert_not_called()
    assert list_volumes(conn) == []
    assert len(get_unarchived_packs(conn)) == 2


def test_override_records_percentage_in_volume_event(tmp_path, monkeypatch):
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700,
        iso_bytes=THIN_CD_ISO, allow=True,
    )
    result = orch.stage()
    dvdisaster.augment_iso.assert_called_once()
    [m] = result.manifests
    notes = [e.detail for e in get_events_for_volume(conn, m.volume_id)
             if e.event_type == "NOTE"]
    assert any("4.0%" in n and "--allow-low-redundancy" in n for n in notes)


def test_ample_redundancy_stages_normally(tmp_path, monkeypatch):
    orch, conn, _x, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.CD700, iso_bytes=CD_AUG // 2,
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


# --- #732: a thin part-filled ISO is padded up a rung, not refused ---

@pytest.fixture
def _no_hash(monkeypatch):
    """Skip hashing multi-GB sparse fake ISOs."""
    monkeypatch.setattr("lcsas.burn.orchestrator.sha256_file",
                        lambda _p: "0" * 64)


def _thin_dvd_iso() -> int:
    """A 4.5 GB ISO: ~4.4% parity on the DVD rung (under the floor)."""
    return 4_500_000_000 // 2048 * 2048


def test_thin_dvd_iso_on_bd25_is_padded_up_a_rung(
        tmp_path, monkeypatch, _no_hash):
    orch, conn, xorriso, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.BD25,
        iso_bytes=_thin_dvd_iso(),
    )
    result = orch.stage()
    [m] = result.manifests
    # Mastered twice: once plain, once with padding inside the ISO.
    assert xorriso.create_iso.call_count == 2
    pad = xorriso.create_iso.call_args_list[1].kwargs["padding_bytes"]
    target = (246 * 9000 - 1) * 2048    # first size dvdisaster puts on DVD9
    assert pad == target - _thin_dvd_iso() + XORRISO_DEFAULT_PADDING_BYTES
    # The padded ISO is what gets augmented and recorded.
    dvdisaster.augment_iso.assert_called_once()
    assert m.iso_size_bytes == target
    assert m.iso_path.stat().st_size == target
    notes = [e.detail for e in get_events_for_volume(conn, m.volume_id)
             if e.event_type == "NOTE"]
    assert any("#732" in n and "padded" in n for n in notes)
    assert not any("--allow-low-redundancy" in n for n in notes)


def test_ample_parity_on_bd25_is_not_padded(tmp_path, monkeypatch, _no_hash):
    orch, _conn, xorriso, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.BD25,
        iso_bytes=4_400_000_000 // 2048 * 2048,    # ~6.8% on DVD
    )
    orch.stage()
    assert xorriso.create_iso.call_count == 1
    dvdisaster.augment_iso.assert_called_once()


def test_thin_on_the_media_own_rung_is_still_refused(tmp_path, monkeypatch):
    # ~4.8% on the BD rung of a BD25: no higher rung fits the disc.
    orch, conn, xorriso, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.BD25,
        iso_bytes=23_100_000_000 // 2048 * 2048,
    )
    with pytest.raises(LowRedundancyError):
        orch.stage()
    assert xorriso.create_iso.call_count == 1
    dvdisaster.augment_iso.assert_not_called()
    assert list_volumes(conn) == []


def test_short_padding_fails_loud(tmp_path, monkeypatch):
    """xorriso ignoring -padding must not slip a thin ISO through."""
    orch, conn, xorriso, dvdisaster, _c = _env(
        tmp_path, monkeypatch, media=MediaType.BD25,
        iso_bytes=_thin_dvd_iso(),
    )

    def _ignores_padding(source_dir, output_iso, volume_label, **kwargs):
        with open(output_iso, "wb") as f:
            f.truncate(_thin_dvd_iso())
        return Path(output_iso)

    xorriso.create_iso.side_effect = _ignores_padding
    with pytest.raises(RuntimeError, match="short of"):
        orch.stage()
    dvdisaster.augment_iso.assert_not_called()
    assert list_volumes(conn) == []
