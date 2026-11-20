"""#862: the ``consolidate`` preview counts volumes as ``--execute`` stages.

The preview estimated the volume count from the configured
``metadata_reserve_mb`` alone; the stage packs with the measured disc
metadata (#811) and first-fit-decreasing.  With a small configured
reserve the preview promised fewer volumes than ``--execute`` staged.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from lcsas.binpack.algorithm import estimate_volumes_needed
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume

PACK_BYTES = 300_000
PACKS = 4


def _fake_create_iso(self, source_dir, output_iso, volume_label, **kwargs):
    Path(output_iso).write_bytes(b"\x00" * 1024)
    return Path(output_iso)


def _archive(tmp_path: Path) -> tuple[str, Path]:
    """Two VERIFIED TEST_TINY volumes, 1.2 MB of packs in all, and a
    config with no metadata reserve of its own."""
    mirror = tmp_path / "mirror" / "test"
    (mirror / "data").mkdir(parents=True)
    for sub in ("index", "snapshots", "keys"):
        (mirror / sub).mkdir()
        (mirror / sub / "dummy.json").write_text('{"test": true}')
    (mirror / "config").write_text('{"version": 2}')
    staging = tmp_path / "staging"
    staging.mkdir()
    db = tmp_path / "catalog.db"

    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "test", "Test", str(mirror))
    create_location(conn, "Home")
    for v in range(2):
        vol = create_volume(
            conn, label=f"OLD_{v}", uuid=f"uuid-old-{v}",
            media_type="TEST_TINY", capacity_bytes=2_097_152,
            status="VERIFIED",
        )
        pack_ids = []
        for p in range(PACKS // 2):
            seed = f"volume {v} pack {p} ".encode()
            content = (seed * (PACK_BYTES // len(seed) + 1))[:PACK_BYTES]
            sha = hashlib.sha256(content).hexdigest()
            (mirror / "data" / sha).write_bytes(content)
            pack_ids.append(register_pack(
                conn, sha256=sha, size_bytes=len(content), repo_id="test",
            ).pack_id)
        bulk_link_packs(conn, vol.volume_id, pack_ids)
        add_volume_copy(conn, vol.volume_id, "Home", commit=False)
    conn.commit()
    conn.close()

    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        "[paths]\n"
        f'mirror_base = "{mirror.parent}"\n'
        f'staging = "{staging}"\n'
        f'catalog = "{db}"\n'
        "\n"
        "[defaults]\n"
        'media_type = "TEST_TINY"\n'
        'label_prefix = "NEW"\n'
        'location = "Home"\n'
        "metadata_reserve_mb = 0\n"
        "allow_unverified_repo_format = true\n"
        "\n"
        "[repos.test]\n"
        f'mirror_path = "{mirror}"\n'
    )
    return str(cfg), db


def _consolidate(cfg: str, *extra: str) -> int:
    with (
        patch("builtins.input", MagicMock(return_value="yes")),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.create_iso",
              _fake_create_iso),
    ):
        return main(["--config", cfg, "consolidate", "OLD_0", "OLD_1",
                     "--media-type", "TEST_TINY", *extra])


def test_preview_volume_count_is_what_execute_stages(tmp_path, caplog):
    cfg, db = _archive(tmp_path)
    # What the old preview said: the packs fit one disc with no reserve.
    assert estimate_volumes_needed(
        PACK_BYTES * PACKS, MediaType.TEST_TINY.capacity_bytes,
        reserved=0, ecc_overhead_pct=MediaType.TEST_TINY.ecc_overhead_pct,
    ) == 1

    assert _consolidate(cfg) == 0, caplog.text
    [shown] = re.findall(r"Volumes needed: (\d+)", caplog.text)
    caplog.clear()

    assert _consolidate(cfg, "--execute") == 0, caplog.text
    conn = get_connection(db)
    try:
        staged = conn.execute(
            "SELECT COUNT(*) FROM volumes WHERE label LIKE 'NEW%'"
        ).fetchone()[0]
    finally:
        conn.close()
    # The measured disc metadata leaves room for fewer packs per disc.
    assert staged > 1
    assert int(shown) == staged
    assert f"across {staged} volume(s)" in caplog.text
