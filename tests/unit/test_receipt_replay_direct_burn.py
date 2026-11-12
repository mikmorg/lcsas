"""A direct burn's receipt replayed on the same catalog is already imported (#727).

``lcsas burn`` writes a durable burn receipt per disc under
``<db dir>/receipts/``.  ``lcsas catalog import-receipts`` replays those
onto a rebuilt catalog, and must see, on the catalog that did the burn,
that the receipt is already recorded: the disc record carries the
receipt's burn date, and so do the burn's verify events.  The burn used
to take two different ``datetime.now()`` values, so nothing matched and
the receipt was imported again.

The burn path is the real orchestrator; xorriso and the drive are faked.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.locations import create_location
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all


def _config(tmp_path: Path) -> LCSASConfig:
    mirror = tmp_path / "mirror"
    repo_dir = mirror / "family"
    (repo_dir / "data").mkdir(parents=True)
    for sub in ("index", "snapshots", "keys"):
        (repo_dir / sub).mkdir()
        (repo_dir / sub / "dummy.json").write_text('{"test": true}')
    (repo_dir / "config").write_text('{"version": 2}')
    staging = tmp_path / "staging"
    staging.mkdir()
    return LCSASConfig(
        mirror_base_path=mirror,
        staging_path=staging,
        db_path=tmp_path / "db" / "archive.db",
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=100,
        label_prefix="TEST",
        repositories={"family": RepositoryConfig(name="family",
                                                 mirror_path=repo_dir)},
        allow_unverified_repo_format=True,
    )


def _orchestrator(config: LCSASConfig, conn, *, disc_ok: bool = True,
                  label: str = "") -> BurnOrchestrator:
    """The xorriso fake masters a 1 KiB zero ISO; the 'drive' reads back
    zeros, so the device hash matches.  *disc_ok* False fails the
    -check_media step."""
    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    xorriso.verify_disc.return_value = disc_ok
    xorriso.read_disc_volume_id.return_value = label
    return BurnOrchestrator(
        config, conn, xorriso, MagicMock(),
        device_reader=lambda device, n: hashlib.sha256(b"\x00" * n).hexdigest(),
        prompt=NullOperatorPrompt(),
    )


@pytest.fixture
def env(tmp_path):
    config = _config(tmp_path)
    config.db_path.parent.mkdir()
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    create_location(conn, "Home_Shelf")
    data_dir = config.repositories["family"].mirror_path / "data"
    content = b"receipt-replay-pack" * 4
    sha = hashlib.sha256(content).hexdigest()
    register_pack(conn, sha256=sha, size_bytes=len(content), repo_id="family")
    (data_dir / sha).write_bytes(content)
    conn.commit()
    session = _orchestrator(config, conn).stage()
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'database = "{config.db_path}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    yield {"config": config, "cfg": cfg, "conn": conn,
           "session_id": session.session_id,
           "label": session.manifests[0].volume_label,
           "tree": session.staging_dir / session.manifests[0].volume_label}
    conn.close()


def _burn(env, *, skip_burn: bool, disc_ok: bool = True) -> Path:
    orch = _orchestrator(env["config"], env["conn"], disc_ok=disc_ok,
                         label=env["label"])
    with patch.object(BurnOrchestrator, "_assert_blank_media"):
        orch.burn_session(env["session_id"], location="Home_Shelf",
                          device="/dev/null", skip_burn=skip_burn,
                          interactive=False)
    receipts = sorted((env["config"].db_path.parent / "receipts").glob("*.json"))
    assert len(receipts) == 1
    return receipts[0]


def _catalog_state(db: Path) -> tuple[list[tuple], ...]:
    conn = get_connection(db)
    try:
        return tuple(
            [tuple(r) for r in conn.execute(sql).fetchall()]
            for sql in (
                "SELECT * FROM volumes ORDER BY volume_id",
                "SELECT * FROM volume_copies ORDER BY id",
                "SELECT * FROM volume_events ORDER BY event_id",
            )
        )
    finally:
        conn.close()


def _import(env, receipt: Path, caplog) -> str:
    caplog.clear()
    assert main(["--config", str(env["cfg"]), "catalog", "import-receipts",
                 str(receipt)]) == 0, caplog.text
    return caplog.text


@pytest.mark.parametrize("skip_burn", [True, False])
def test_direct_burn_receipt_is_already_imported(env, caplog, skip_burn):
    receipt = _burn(env, skip_burn=skip_burn)
    assert json.loads(receipt.read_text())["verify_passed"] is True
    before = _catalog_state(env["config"].db_path)
    out = _import(env, receipt, caplog)
    assert "already imported" in out
    assert "Imported 0 receipt(s)" in out
    assert _catalog_state(env["config"].db_path) == before


def test_failed_disc_receipt_is_already_imported(env, caplog):
    """No disc is recorded for a failed verify; the VERIFY_FAIL event,
    dated with the receipt's burn date, is what matches."""
    receipt = _burn(env, skip_burn=False, disc_ok=False)
    assert json.loads(receipt.read_text())["verify_passed"] is False
    before = _catalog_state(env["config"].db_path)
    out = _import(env, receipt, caplog)
    assert "already imported" in out
    assert _catalog_state(env["config"].db_path) == before


def test_older_receipt_after_reburn_at_same_location(env, caplog):
    """A second disc burned at the same location replaces the disc
    record; the first disc's receipt still matches its VERIFY_PASS."""
    first = _burn(env, skip_burn=False)
    kept = first.parent.parent / "first-disc.json"
    shutil.copy(first, kept)
    first.unlink()
    _burn(env, skip_burn=False)
    before = _catalog_state(env["config"].db_path)
    out = _import(env, kept, caplog)
    assert "already imported" in out
    assert _catalog_state(env["config"].db_path) == before


def test_receipt_already_on_a_later_disc_catalog(env, caplog, tmp_path):
    """A catalog rebuilt from a disc staged AFTER the burn carries the
    burn's disc record and verify events (rebuild merges both from the
    same disc catalog), so the receipt is already imported there too."""
    receipt = _burn(env, skip_burn=False)
    data_dir = env["config"].repositories["family"].mirror_path / "data"
    content = b"second-session-pack" * 4
    sha = hashlib.sha256(content).hexdigest()
    register_pack(env["conn"], sha256=sha, size_bytes=len(content),
                  repo_id="family")
    (data_dir / sha).write_bytes(content)
    env["conn"].commit()
    later = _orchestrator(env["config"], env["conn"]).stage()
    later_tree = later.staging_dir / later.manifests[0].volume_label

    rebuilt = tmp_path / "rebuilt" / "archive.db"
    rebuilt.parent.mkdir()
    assert main(["catalog", "rebuild", str(later_tree),
                 "--output", str(rebuilt)]) == 0
    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(env["cfg"].read_text().replace(
        str(env["config"].db_path), str(rebuilt)))
    before = _catalog_state(rebuilt)
    caplog.clear()
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 str(receipt)]) == 0, caplog.text
    assert "already imported" in caplog.text
    assert _catalog_state(rebuilt) == before
    conn = get_connection(rebuilt)
    try:
        row = conn.execute(
            "SELECT c.status FROM volume_copies c JOIN volumes v "
            "USING (volume_id) WHERE v.label = ?", (env["label"],),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "ACTIVE"


def test_receipt_still_imports_on_rebuilt_catalog(env, caplog, tmp_path):
    """The disc catalog was copied before the burn, so a catalog rebuilt
    from the disc has no disc record: the receipt imports there."""
    receipt = _burn(env, skip_burn=False)
    rebuilt = tmp_path / "rebuilt" / "archive.db"
    rebuilt.parent.mkdir()
    assert main(["catalog", "rebuild", str(env["tree"]),
                 "--output", str(rebuilt)]) == 0
    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(env["cfg"].read_text().replace(
        str(env["config"].db_path), str(rebuilt)))
    caplog.clear()
    # The volume's own disc catalog was copied before its ISO was hashed,
    # so the rebuilt catalog has no staged hash to check the receipt
    # against: --allow-unhashed, as the import's message says.
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 "--allow-unhashed", str(receipt)]) == 0, caplog.text
    assert "Imported 1 receipt(s)" in caplog.text
    conn = get_connection(rebuilt)
    try:
        row = conn.execute(
            "SELECT c.status, c.burn_date, v.status FROM volume_copies c "
            "JOIN volumes v USING (volume_id) WHERE v.label = ?",
            (env["label"],),
        ).fetchone()
    finally:
        conn.close()
    assert tuple(row) == (
        "ACTIVE", json.loads(receipt.read_text())["burn_date"], "VERIFIED",
    )
