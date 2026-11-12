"""A lost volume that gets a disc back is back in service (#726).

A lost volume (its last in-service disc recorded lost, destroyed or out
of service) is recorded as DEPRECATED (ADR 0003).  Burning it again from
its burn session, importing a later burn receipt for it, or recording a
verified disc with ``verify --disc --location`` makes it VERIFIED again
(BURNED for a disc nobody verified), so ``lcsas status``, the stage pool
and the redundancy report count it.  A volume deprecated by consolidation
never comes back this way.

The burn path is the real orchestrator; only xorriso and the drive are
faked.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
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
from lcsas.db.queries import (
    get_lost_volume_ids,
    get_redundancy_report,
    get_unarchived_packs,
)
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import add_volume_copy, destroy_copy
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import (
    create_volume,
    get_volume_by_id,
    get_volume_by_label,
    update_status,
)
from lcsas.utils.labels import generate_uuid


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
        db_path=tmp_path / "archive.db",
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=100,
        label_prefix="TEST",
        repositories={"family": RepositoryConfig(name="family",
                                                 mirror_path=repo_dir)},
        allow_unverified_repo_format=True,
    )


def _orchestrator(config: LCSASConfig, conn) -> BurnOrchestrator:
    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    return BurnOrchestrator(
        config, conn, xorriso, MagicMock(),
        device_reader=lambda device, n: hashlib.sha256(b"\x00" * n).hexdigest(),
        prompt=NullOperatorPrompt(),
    )


@pytest.fixture
def burned(tmp_path):
    """One volume staged and burned (verified) at Home, then its only disc
    recorded lost with the CLI.  Returns the catalog, its config and the
    orchestrator pieces to burn it again."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    create_location(conn, "Home")
    data_dir = config.repositories["family"].mirror_path / "data"
    content = b"lost-volume-pack" * 4
    sha = hashlib.sha256(content).hexdigest()
    register_pack(conn, sha256=sha, size_bytes=len(content), repo_id="family")
    (data_dir / sha).write_bytes(content)
    conn.commit()
    orch = _orchestrator(config, conn)
    session = orch.stage()
    orch.burn_session(session.session_id, location="Home", skip_burn=True)
    label = session.manifests[0].volume_label
    conn.close()

    assert main(["--db", str(config.db_path), "copy", "destroy", "--lost",
                 label, "Home"]) == 0
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'database = "{config.db_path}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    conn = get_connection(config.db_path)
    vol = get_volume_by_label(conn, label)
    assert vol is not None and vol.status == "DEPRECATED"
    assert vol.volume_id in get_lost_volume_ids(conn)
    yield {"config": config, "cfg": cfg, "conn": conn, "label": label,
           "volume_id": vol.volume_id, "session_id": session.session_id}
    conn.close()


def _assert_in_service(conn, volume_id: int, status: str = "VERIFIED") -> None:
    vol = get_volume_by_id(conn, volume_id)
    assert vol.status == status
    assert volume_id not in get_lost_volume_ids(conn)
    # The stage pool no longer holds its packs, and the redundancy
    # report counts its disc.
    assert get_unarchived_packs(conn) == []
    assert get_redundancy_report(conn, min_copies=1) == []
    notes = [e.detail for e in get_events_for_volume(conn, volume_id, "NOTE")]
    assert any(n.startswith(f"Status changed: DEPRECATED → {status}; this "
                            "lost volume has a disc in service again")
               for n in notes), notes


def _status_line(capsys, db: Path, label: str) -> str:
    capsys.readouterr()
    assert main(["--db", str(db), "status"]) == 0
    captured = capsys.readouterr()
    out = captured.out + captured.err
    return next(line for line in out.splitlines() if label in line)


def test_lost_volume_burned_again_is_back_in_service(burned, capsys):
    db = burned["config"].db_path
    assert "LOST" in _status_line(capsys, db, burned["label"])
    # Before the fix the burn refused: DEPRECATED → BURNING is invalid.
    orch = _orchestrator(burned["config"], burned["conn"])
    receipts = orch.burn_session(burned["session_id"], location="Home",
                                 skip_burn=True)
    assert [r.verify_passed for r in receipts] == [True]
    _assert_in_service(burned["conn"], burned["volume_id"])
    line = _status_line(capsys, db, burned["label"])
    assert "VERIFIED" in line and "Home" in line
    assert "LOST" not in line and "DEPRECATED" not in line


def test_lost_volume_failed_reburn_stays_lost(burned):
    orch = _orchestrator(burned["config"], burned["conn"])
    with (
        patch.object(BurnOrchestrator, "_assert_blank_media"),
        patch.object(BurnOrchestrator, "_verify_burned_disc",
                     return_value=False),
    ):
        receipts = orch.burn_session(burned["session_id"], location="Home",
                                     device="/dev/null", interactive=False)
    assert [r.verify_passed for r in receipts] == [False]
    conn = burned["conn"]
    assert get_volume_by_id(conn, burned["volume_id"]).status == "DEPRECATED"
    assert burned["volume_id"] in get_lost_volume_ids(conn)
    assert get_events_for_volume(conn, burned["volume_id"],
                                 "VERIFY_FAIL_REBURN")


@pytest.mark.parametrize("verify_passed, status",
                         [(True, "VERIFIED"), (None, "BURNED")])
def test_lost_volume_receipt_import_brings_it_back(
    burned, tmp_path, verify_passed, status,
):
    """A burn-iso receipt for a disc burned after the loss was recorded."""
    conn = burned["conn"]
    iso_sha = conn.execute(
        "SELECT iso_sha256 FROM session_volumes WHERE volume_id = ?",
        (burned["volume_id"],),
    ).fetchone()[0]
    receipt = tmp_path / "receipt.json"
    receipt.write_text(json.dumps({
        "volume_label": burned["label"],
        "location": "Home",
        "burn_date": (datetime.now(UTC) + timedelta(seconds=5)).isoformat(),
        "iso_sha256": iso_sha,
        "verify_passed": verify_passed,
    }))
    assert main(["--config", str(burned["cfg"]), "catalog",
                 "import-receipts", str(receipt)]) == 0
    _assert_in_service(conn, burned["volume_id"], status)


def test_lost_volume_verify_disc_at_new_location_brings_it_back(burned):
    db = burned["config"].db_path
    create_location(burned["conn"], "Offsite")
    with (
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
              "read_disc_volume_id", return_value=burned["label"]),
        patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
              return_value=True),
        patch("lcsas.burn.device_verify.read_device_sha256",
              side_effect=lambda device, n:
              hashlib.sha256(b"\x00" * n).hexdigest()),
    ):
        assert main(["--db", str(db), "verify", burned["label"], "--disc",
                     "--device", "/dev/null", "--location", "Offsite"]) == 0
    _assert_in_service(burned["conn"], burned["volume_id"])


# ────────────────────────────────────────────────────────────────────
#  A volume deprecated by consolidation never comes back
# ────────────────────────────────────────────────────────────────────


@pytest.fixture
def cat(conn):
    create_location(conn, "Home")
    create_location(conn, "Offsite")
    return conn


def _vol(conn, label):
    return create_volume(
        conn, label=label, uuid=generate_uuid(), media_type="BD25",
        capacity_bytes=25_000_000_000, status="VERIFIED",
    )


def test_consolidated_volume_gets_no_status_back(cat):
    vol = _vol(cat, "OLD")
    add_volume_copy(cat, vol.volume_id, "Home")
    update_status(cat, vol.volume_id, "CONSOLIDATING")
    update_status(cat, vol.volume_id, "DEPRECATED")
    destroy_copy(cat, vol.volume_id, "Home")
    add_volume_copy(cat, vol.volume_id, "Home",
                    last_verified_at=datetime.now(UTC).isoformat())
    assert get_volume_by_id(cat, vol.volume_id).status == "DEPRECATED"


def test_lost_revived_then_consolidated_is_not_lost(cat):
    """Lost, back in service, then consolidated: the newest deprecation
    (consolidation) decides.  Its discs going later neither make it LOST
    nor let a recorded disc bring it back."""
    vol = _vol(cat, "V")
    add_volume_copy(cat, vol.volume_id, "Home")
    destroy_copy(cat, vol.volume_id, "Home", lost=True)
    assert vol.volume_id in get_lost_volume_ids(cat)
    add_volume_copy(cat, vol.volume_id, "Offsite",
                    last_verified_at=datetime.now(UTC).isoformat())
    assert get_volume_by_id(cat, vol.volume_id).status == "VERIFIED"

    update_status(cat, vol.volume_id, "CONSOLIDATING")
    update_status(cat, vol.volume_id, "DEPRECATED")
    destroy_copy(cat, vol.volume_id, "Offsite")
    assert vol.volume_id not in get_lost_volume_ids(cat)
    add_volume_copy(cat, vol.volume_id, "Offsite",
                    last_verified_at=datetime.now(UTC).isoformat())
    assert get_volume_by_id(cat, vol.volume_id).status == "DEPRECATED"


def test_revived_volume_lost_again_is_lost(cat):
    vol = _vol(cat, "V")
    add_volume_copy(cat, vol.volume_id, "Home")
    destroy_copy(cat, vol.volume_id, "Home", lost=True)
    add_volume_copy(cat, vol.volume_id, "Offsite")
    assert get_volume_by_id(cat, vol.volume_id).status == "BURNED"
    assert destroy_copy(cat, vol.volume_id, "Offsite", lost=True)
    assert get_volume_by_id(cat, vol.volume_id).status == "DEPRECATED"
    assert vol.volume_id in get_lost_volume_ids(cat)
