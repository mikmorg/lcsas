"""#676: a volume label number is never issued twice.

The highest number ever issued lives in the catalog header
(``PRAGMA user_version``), so ``session abort`` or a volume delete does
not free it, and a catalog rebuilt from discs keeps it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.label_mark import (
    label_high_water,
    label_seq,
    next_label_seq,
)
from lcsas.db.rebuild import rebuild_catalog
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all, get_schema_version
from lcsas.db.volumes import create_volume, delete_volume
from tests.unit.test_session_pipeline import (
    _make_config,
    _seed_packs,
    _wire_drive_identity,
    _zeros_device_reader,
)


@pytest.fixture
def orch_env(tmp_path):
    config = _make_config(tmp_path, num_repos=1)
    conn = get_memory_connection()
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    # Two volumes' worth on TEST_TINY (2 MB): 3 packs of 800 KB.
    _seed_packs(conn, config, num_packs=3, pack_size=800_000)
    xorriso = MagicMock()

    def _create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _create_iso
    _wire_drive_identity(xorriso)
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_zeros_device_reader,
                            prompt=NullOperatorPrompt())
    return orch, conn


def _seqs(result) -> list[int]:
    return [label_seq(m.volume_label) or 0 for m in result.manifests]


def test_stage_abort_stage_issues_higher_labels(orch_env):
    orch, conn = orch_env
    version_before = get_schema_version(conn)
    first = orch.stage()
    assert len(first.manifests) >= 2
    first_seqs = _seqs(first)

    orch.abort_session(first.session_id)
    second = orch.stage()

    assert min(_seqs(second)) > max(first_seqs)
    assert label_high_water(conn) == max(_seqs(second))
    assert get_schema_version(conn) == version_before  # no schema change


def test_deleted_volume_number_is_not_reused():
    conn = get_memory_connection()
    create_all(conn)
    assert next_label_seq(conn, "LCSAS") == 1
    orch_seq = next_label_seq(conn, "LCSAS")
    vol = create_volume(conn, f"LCSAS_BD25_2026_{orch_seq:04d}", "u1",
                        "BD25", 25, "Home_Shelf", "STAGING", commit=False)
    from lcsas.db.label_mark import raise_label_high_water
    raise_label_high_water(conn, orch_seq)
    conn.commit()
    delete_volume(conn, vol.volume_id)
    assert next_label_seq(conn, "LCSAS") == 2


def test_mark_never_lowers():
    conn = get_memory_connection()
    create_all(conn)
    from lcsas.db.label_mark import raise_label_high_water
    raise_label_high_water(conn, 9)
    raise_label_high_water(conn, 4)
    assert label_high_water(conn) == 9


def _disc(tmp_path: Path, name: str, labels: list[str], mark: int) -> Path:
    disc = tmp_path / name
    disc.mkdir()
    conn = get_connection(disc / "catalog.db")
    create_all(conn)
    for i, label in enumerate(labels):
        create_volume(conn, label, f"{name}-{i}", "BD25", 25, "Home_Shelf",
                      "VERIFIED")
    conn.execute(f"PRAGMA user_version = {mark}")
    conn.commit()
    conn.close()
    return disc


def test_rebuild_keeps_mark_at_least_highest_disc_label(tmp_path):
    # Disc A: labels 1-2, but its catalog had issued up to 12 (aborted
    # sessions).  Disc B: an older catalog with no mark, label 7.
    a = _disc(tmp_path, "a", ["LCSAS_BD25_2026_0001", "LCSAS_BD25_2026_0002"], 12)
    b = _disc(tmp_path, "b", ["LCSAS_BD25_2026_0007"], 0)
    out = tmp_path / "rebuilt.db"

    result = rebuild_catalog([a, b], out)
    assert result.discs_processed == 2

    conn = get_connection(out)
    try:
        assert label_high_water(conn) >= 12
        assert next_label_seq(conn, "LCSAS") == 13
    finally:
        conn.close()


def test_rebuild_mark_covers_labels_without_any_mark(tmp_path):
    b = _disc(tmp_path, "b", ["LCSAS_BD25_2026_0020"], 0)
    out = tmp_path / "rebuilt.db"
    rebuild_catalog([b], out)
    conn = get_connection(out)
    try:
        assert label_high_water(conn) >= 20
    finally:
        conn.close()
