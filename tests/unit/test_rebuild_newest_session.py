"""#678: a catalog rebuilt from discs, and the newest burn session.

Each disc carries the catalog as it was when the disc was staged, so in
every input catalog the newest burn session's volumes are STAGING with no
disc record.  The rebuild marks a volume BURNED when its own disc was one
of the inputs and lists it; ``lcsas verify <label> --disc --location X``
then records the disc and marks the volume VERIFIED.  At no point may the
next ``lcsas stage`` select those volumes' packs again.

The discs here are the staging trees the real orchestrator builds (each
holds the disc catalog and ``volume_info.json``, exactly what is mastered
into the ISO); only xorriso is faked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.packs import register_pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_copies import get_copies_for_volume
from lcsas.db.volumes import get_volume_by_label


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


def _add_packs(conn, config: LCSASConfig, first: int, count: int,
               size: int) -> list[str]:
    shas = []
    data_dir = config.repositories["family"].mirror_path / "data"
    for i in range(first, first + count):
        seed = f"{i:08x}".encode()
        content = (seed * (size // len(seed) + 1))[:size]
        sha = hashlib.sha256(content).hexdigest()
        register_pack(conn, sha256=sha, size_bytes=size, repo_id="family")
        (data_dir / sha).write_bytes(content)
        shas.append(sha)
    conn.commit()
    return shas


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
def discs(tmp_path):
    """Session A (one volume, burned and verified at Home), then session B
    (two volumes, staged and burned, but the catalog is lost before any
    record of B's burn reaches a disc).  Returns the three disc trees, the
    labels of session B and the config."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.commit()
    orch = _orchestrator(config, conn)

    _add_packs(conn, config, 1, 1, 50)
    session_a = orch.stage()
    orch.burn_session(session_a.session_id, location="Home", skip_burn=True)

    # 3 packs of 800 kB do not fit one 2 MiB TEST_TINY disc: two volumes.
    _add_packs(conn, config, 2, 3, 800_000)
    session_b = orch.stage()
    assert len(session_b.manifests) == 2
    conn.close()

    trees = [session_a.staging_dir / m.volume_label
             for m in session_a.manifests]
    trees += [session_b.staging_dir / m.volume_label
              for m in session_b.manifests]
    for tree in trees:
        assert (tree / "catalog.db").is_file()
        assert (tree / "volume_info.json").is_file()
    labels_b = sorted(m.volume_label for m in session_b.manifests)
    return trees, labels_b, config


def _stage_selects_nothing(config: LCSASConfig, db: Path,
                           location: str | None = None) -> None:
    conn = get_connection(db)
    try:
        assert get_unarchived_packs(conn) == []
        orch = _orchestrator(config, conn)
        with pytest.raises(ValueError, match="No packs need staging"):
            orch.stage(for_location=location, dry_run=True)
    finally:
        conn.close()


def test_rebuild_marks_read_discs_burned_and_lists_them(discs, tmp_path, caplog):
    trees, labels_b, config = discs
    out = tmp_path / "rebuilt.db"

    caplog.clear()
    rc = main(["catalog", "rebuild", *[str(t) for t in trees],
               "--output", str(out)])
    assert rc == 0, caplog.text

    conn = get_connection(out)
    try:
        statuses = {
            row["label"]: row["status"]
            for row in conn.execute("SELECT label, status FROM volumes")
        }
        # No volume whose disc was read is left STAGING.
        assert "STAGING" not in statuses.values()
        for label in labels_b:
            assert statuses[label] == "BURNED"
            vol = get_volume_by_label(conn, label)
            assert get_copies_for_volume(conn, vol.volume_id) == []
        # Session A's burn is on session B's discs: VERIFIED, at Home.
        (label_a,) = set(statuses) - set(labels_b)
        assert statuses[label_a] == "VERIFIED"
    finally:
        conn.close()

    # The output names the volumes and the command for each.
    for label in labels_b:
        assert f"lcsas verify {label} --disc --location" in caplog.text
    (listing,) = [line for line in caplog.text.splitlines()
                  if "were marked BURNED" in line]
    assert label_a not in listing

    # Already now the next stage selects none of their packs.
    _stage_selects_nothing(config, out)


def test_rebuild_api_reports_needs_verify(discs, tmp_path):
    from lcsas.db.rebuild import rebuild_catalog

    trees, labels_b, _config_ = discs
    result = rebuild_catalog(trees, tmp_path / "rebuilt.db")
    assert result.ok
    assert result.needs_verify == labels_b


def test_rebuild_from_an_older_disc_alone(discs, tmp_path):
    """Only a disc that was read proves a burn: from session A's disc
    alone, A (STAGING in its own catalog) is promoted and listed; session
    B's volumes are unknown to that catalog."""
    from lcsas.db.rebuild import rebuild_catalog

    trees, labels_b, _config_ = discs
    out = tmp_path / "rebuilt.db"
    result = rebuild_catalog(trees[:1], out)
    assert result.ok
    assert len(result.needs_verify) == 1
    assert not set(result.needs_verify) & set(labels_b)


def test_verify_disc_location_records_the_disc_and_verifies(
    discs, tmp_path, caplog,
):
    trees, labels_b, config = discs
    out = tmp_path / "rebuilt.db"
    assert main(["catalog", "rebuild", *[str(t) for t in trees],
                 "--output", str(out)]) == 0

    for label in labels_b:
        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value=label),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            # A disc whose bytes are the fake all-zeros ISO.  Session B's
            # first volume has its ISO hash in the second disc's catalog,
            # so the device read-back really is compared.
            patch("lcsas.burn.device_verify.read_device_sha256",
                  side_effect=lambda device, n:
                  hashlib.sha256(b"\x00" * n).hexdigest()),
        ):
            rc = main(["--db", str(out), "verify", label, "--disc",
                       "--device", "/dev/null", "--location", "Home"])
        assert rc == 0, caplog.text

    conn = get_connection(out)
    try:
        for label in labels_b:
            vol = get_volume_by_label(conn, label)
            assert vol.status == "VERIFIED"
            copies = get_copies_for_volume(conn, vol.volume_id)
            assert [(c.location, c.status) for c in copies] == [
                ("Home", "ACTIVE")
            ]
            assert copies[0].last_verified_at
            events = [r["event_type"] for r in conn.execute(
                "SELECT event_type FROM volume_events WHERE volume_id = ?",
                (vol.volume_id,),
            )]
            assert "VERIFY_PASS" in events
    finally:
        conn.close()

    _stage_selects_nothing(config, out)
    _stage_selects_nothing(config, out, location="Home")


def test_verify_disc_unknown_location_records_nothing(discs, tmp_path, caplog):
    trees, labels_b, _config_ = discs
    out = tmp_path / "rebuilt.db"
    assert main(["catalog", "rebuild", *[str(t) for t in trees],
                 "--output", str(out)]) == 0
    label = labels_b[0]

    read_id = MagicMock(return_value=label)
    with patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
               "read_disc_volume_id", read_id):
        rc = main(["--db", str(out), "verify", label, "--disc",
                   "--device", "/dev/null", "--location", "Hoem"])
    assert rc == 1
    assert "Unknown location 'Hoem'" in caplog.text
    read_id.assert_not_called()

    conn = get_connection(out)
    try:
        vol = get_volume_by_label(conn, label)
        assert vol.status == "BURNED"
        assert get_copies_for_volume(conn, vol.volume_id) == []
        assert conn.execute(
            "SELECT 1 FROM locations WHERE name = 'Hoem'"
        ).fetchone() is None
    finally:
        conn.close()


def test_receipts_import_after_rebuild_with_allow_unhashed(
    discs, tmp_path, caplog,
):
    """RECOVER.txt's OWNER HABIT: after a rebuild, the newest session's
    receipts are imported with --allow-unhashed (the rebuilt catalog has
    no ISO hash for the newest discs) and the volumes become VERIFIED at
    the receipt's location."""
    import json

    trees, labels_b, _config_obj = discs
    out = tmp_path / "rebuilt.db"
    assert main(["catalog", "rebuild", *[str(t) for t in trees],
                 "--output", str(out)]) == 0

    conn = get_connection(out)
    try:
        staged_for = {
            label: get_volume_by_label(conn, label).location
            for label in labels_b
        }
    finally:
        conn.close()
    # The receipts carry the hash of the ISO that was burned: the one
    # recorded at staging in the (since lost) original catalog.
    orig = get_connection(_config_obj.db_path)
    try:
        iso_hash = {
            r["label"]: r["iso_sha256"] for r in orig.execute(
                "SELECT v.label, sv.iso_sha256 FROM session_volumes sv "
                "JOIN volumes v USING (volume_id)")
        }
    finally:
        orig.close()
    receipts = []
    for label in labels_b:
        loc = staged_for[label] or "Home"
        r = tmp_path / f"{label}.json"
        r.write_text(json.dumps({
            "volume_label": label, "location": loc, "session_id": "s",
            "device": "/dev/sr0", "burn_date": "2026-11-11T00:00:00+00:00",
            "iso_sha256": iso_hash[label],
            "verify_passed": True,
        }))
        receipts.append(str(r))

    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{out}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    # The discs' location must be registered (RECOVER.txt: 'location add').
    for loc in {v or "Home" for v in staged_for.values()}:
        main(["--config", str(cfg), "location", "add", loc])
    # Without --allow-unhashed the receipts are refused (no staged hash).
    caplog.clear()
    assert main(["--config", str(cfg), "catalog", "import-receipts",
                 *receipts]) == 1
    rc = main(["--config", str(cfg), "catalog", "import-receipts",
               "--allow-unhashed", *receipts])
    assert rc == 0, caplog.text
    conn = get_connection(out)
    try:
        for label in labels_b:
            vol = get_volume_by_label(conn, label)
            assert vol.status == "VERIFIED"
            copies = get_copies_for_volume(conn, vol.volume_id)
            assert [c.location for c in copies] == [staged_for[label] or "Home"]
    finally:
        conn.close()
