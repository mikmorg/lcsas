"""``session abort`` and ``stage --clean`` never delete a burned volume.

A catalog rebuilt from discs records the newest burn session's volumes
as BURNED with no disc record (#678): the disc catalogs were copied at
staging time.  Both commands used to treat "no disc record" as "never
burned" and delete such volumes, with their ISOs and staging tree, under
``--force`` (``session abort`` always forces).  They now refuse the whole
burn session, before anything is removed, when a volume of it is BURNED
with no disc record, or STAGING/BURNING with an event that records a
good disc.

The rebuilt catalog here comes from the real orchestrator's staging trees
and the real ``catalog rebuild``; only xorriso is faked.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from lcsas.burn.operator import NullOperatorPrompt
from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.main import main
from lcsas.config.media import MediaType
from lcsas.config.settings import LCSASConfig, RepositoryConfig
from lcsas.db.connection import get_connection
from lcsas.db.lifecycle import VolumeLifecycle
from lcsas.db.packs import register_pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_session
from lcsas.db.volume_events import add_event
from lcsas.db.volume_packs import get_pack_ids_for_volume
from lcsas.db.volumes import get_volume_by_label
from tests.unit.volume_status import force_status


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
        db_path=tmp_path / "catalog.db",
        default_media_type=MediaType.TEST_TINY,
        default_ecc_redundancy_pct=0,
        metadata_reserve_bytes=100,
        label_prefix="TEST",
        repositories={"family": RepositoryConfig(name="family",
                                                 mirror_path=repo_dir)},
        allow_unverified_repo_format=True,
    )


def _add_packs(conn, config: LCSASConfig, first: int, count: int,
               size: int) -> None:
    data_dir = config.repositories["family"].mirror_path / "data"
    for i in range(first, first + count):
        seed = f"{i:08x}".encode()
        content = (seed * (size // len(seed) + 1))[:size]
        sha = hashlib.sha256(content).hexdigest()
        register_pack(conn, sha256=sha, size_bytes=size, repo_id="family")
        (data_dir / sha).write_bytes(content)
    conn.commit()


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
def rebuilt(tmp_path):
    """Session A burned and verified at Home; session B (two volumes)
    staged and burned, then the catalog is lost and rebuilt from all three
    discs.  Returns the rebuilt catalog, a config for it, session B's id,
    labels and staging dir."""
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
    _add_packs(conn, config, 2, 3, 450_000)  # two volumes (#811)
    session_b = orch.stage()
    assert len(session_b.manifests) == 2
    conn.close()

    trees = [session_a.staging_dir / m.volume_label
             for m in session_a.manifests]
    trees += [session_b.staging_dir / m.volume_label
              for m in session_b.manifests]
    out = tmp_path / "rebuilt.db"
    assert main(["catalog", "rebuild", *[str(t) for t in trees],
                 "--output", str(out)]) == 0

    cfg = tmp_path / "rebuilt.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'catalog = "{out}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    return {
        "db": out,
        "cfg": cfg,
        "session_id": session_b.session_id,
        "labels": sorted(m.volume_label for m in session_b.manifests),
        "isos": [m.iso_path for m in session_b.manifests],
        "staging_dir": session_b.staging_dir,
    }


def _assert_untouched(env) -> None:
    conn = get_connection(env["db"])
    try:
        for label in env["labels"]:
            vol = get_volume_by_label(conn, label)
            assert vol is not None, label
            assert vol.status == "BURNED"
            assert get_pack_ids_for_volume(conn, vol.volume_id)
        assert get_unarchived_packs(conn) == []
        assert get_session(conn, env["session_id"]).status != "CLEANED"
    finally:
        conn.close()
    for iso in env["isos"]:
        assert iso.exists(), iso
    assert env["staging_dir"].is_dir()


def test_session_abort_refuses_rebuilt_burned_volumes(rebuilt, caplog):
    caplog.clear()
    rc = main(["--config", str(rebuilt["cfg"]), "session", "abort",
               rebuilt["session_id"]])
    assert rc == 1
    for label in rebuilt["labels"]:
        assert f"{label} is BURNED, but no disc of it is recorded" \
            in caplog.text
    assert "Nothing was deleted" in caplog.text
    assert "--disc --location <where it is kept>" in caplog.text
    _assert_untouched(rebuilt)


@pytest.mark.parametrize("force", [False, True])
def test_stage_clean_refuses_rebuilt_burned_volumes(rebuilt, caplog, force):
    caplog.clear()
    rc = main(["--config", str(rebuilt["cfg"]), "stage", "--clean",
               "--session", rebuilt["session_id"],
               *(["--force"] if force else [])])
    assert rc == 1
    assert "never deleted, even with --force" in caplog.text
    _assert_untouched(rebuilt)


def test_clean_works_once_the_discs_are_recorded(rebuilt, caplog):
    """The refusal's way out: 'verify --disc --location' records each disc,
    after which the burn session cleans as usual and the volumes stay."""
    for label in rebuilt["labels"]:
        with (
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner."
                  "read_disc_volume_id", return_value=label),
            patch("lcsas.iso.xorriso.SubprocessXorrisoRunner.verify_disc",
                  return_value=True),
            patch("lcsas.burn.device_verify.read_device_sha256",
                  side_effect=lambda device, n:
                  hashlib.sha256(b"\x00" * n).hexdigest()),
        ):
            assert main(["--catalog", str(rebuilt["db"]), "verify", label,
                         "--disc", "--device", "/dev/null",
                         "--location", "Home"]) == 0, caplog.text

    assert main(["--config", str(rebuilt["cfg"]), "stage", "--clean",
                 "--session", rebuilt["session_id"]]) == 0, caplog.text
    conn = get_connection(rebuilt["db"])
    try:
        for label in rebuilt["labels"]:
            assert get_volume_by_label(conn, label).status == "VERIFIED"
        assert get_session(conn, rebuilt["session_id"]).status == "CLEANED"
    finally:
        conn.close()
    assert not rebuilt["staging_dir"].exists()


# ---------------------------------------------------------------------
# STAGING volumes: which events count as a burned disc
# ---------------------------------------------------------------------


@pytest.fixture
def staged(tmp_path):
    """One staged, never-burned burn session of one volume."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.execute("INSERT INTO locations (name) VALUES ('Home')")
    conn.commit()
    _add_packs(conn, config, 1, 2, 50)
    orch = _orchestrator(config, conn)
    result = orch.stage()
    (manifest,) = result.manifests
    yield orch, conn, result, manifest
    conn.close()


def _receipt_event(conn, volume_id: int, verify_passed: object) -> None:
    add_event(conn, volume_id, "BURN_RECEIPT_IMPORTED", location="Home",
              detail=json.dumps({"verify_passed": verify_passed}))


@pytest.mark.parametrize("events, evidence", [
    ([("VERIFY_PASS", "Home", "Post-burn read-back")],
     "passed verification at Home"),
    # 'lcsas verify <label> --disc' with no --location records no location.
    ([("VERIFY_PASS", None, "Disc verify")], "passed verification"),
    ([("LOCATION_MOVE", "Home", "")], "was moved at Home"),
    ([("VERIFY_FAIL_REBURN", "Home", "")], "was burned again at Home"),
])
def test_abort_refuses_staging_volume_with_burn_evidence(
    staged, events, evidence,
):
    orch, conn, result, manifest = staged
    for event_type, location, detail in events:
        add_event(conn, manifest.volume_id, event_type, location=location,
                  detail=detail)

    for call in (lambda: orch.abort_session(result.session_id),
                 lambda: orch.clean_session(result.session_id, force=True),
                 lambda: orch.abort_volume(manifest.volume_label)):
        with pytest.raises(ValueError, match=evidence):
            call()
    assert get_volume_by_label(conn, manifest.volume_label) is not None
    assert manifest.iso_path is not None and manifest.iso_path.exists()
    assert get_session(conn, result.session_id).status == "STAGED"


def test_abort_refuses_staging_volume_with_passed_receipt(staged):
    orch, conn, result, manifest = staged
    _receipt_event(conn, manifest.volume_id, True)
    with pytest.raises(ValueError, match="burn receipt of it was imported"):
        orch.abort_session(result.session_id)
    assert get_volume_by_label(conn, manifest.volume_label) is not None


@pytest.mark.parametrize("setup", ["none", "iso_verify", "failed_burn",
                                   "failed_receipt"])
def test_abort_still_deletes_never_burned_and_failed_volumes(staged, setup):
    """No good disc recorded: an ISO verify (no location), a failed
    post-burn verify (#483) and a failed receipt are not evidence."""
    orch, conn, result, manifest = staged
    vid = manifest.volume_id
    if setup == "iso_verify":
        add_event(conn, vid, "VERIFY_PASS", detail="ISO verify: x.iso")
    elif setup == "failed_burn":
        add_event(conn, vid, "VERIFY_FAIL", location="Home",
                  detail="Post-burn read-back failed")
    elif setup == "failed_receipt":
        add_event(conn, vid, "VERIFY_FAIL", location="Home",
                  detail="Offline burn receipt: r.json")
        _receipt_event(conn, vid, False)

    summary = orch.abort_session(result.session_id)
    assert summary.labels == [manifest.volume_label]
    assert get_volume_by_label(conn, manifest.volume_label) is None
    assert len(get_unarchived_packs(conn)) == 2
    assert get_session(conn, result.session_id).status == "CLEANED"


# ---------------------------------------------------------------------
# #904: a volume an older LCSAS moved from BURNED to BURNING
# ---------------------------------------------------------------------

# The notes update_status() wrote in rc1 to rc9 (the rc7 install among
# them): "Status changed: {current} → {status}", unchanged since it was
# added (d24dcbf).  BURNED → BURNING was allowed from BURN-05 (4c74bde,
# before rc1) until #810 (rc10).  Written as literals, not through
# update_status(), so the test keeps the text such catalogs already hold
# even if the writer changes.
_RC7_NOTES_BURNED_THEN_BURNING = (
    "Status changed: STAGING → BURNING",
    "Status changed: BURNING → BURNED",
    "Status changed: BURNED → BURNING",
)
_WAS_BURNED = "it was BURNED when a burn of it started"


def _rc7_history(conn, volume_id: int, status: str,
                 notes: tuple[str, ...]) -> None:
    """Give the volume *status* and *notes*, as the older LCSAS left it."""
    force_status(conn, volume_id, status)
    for detail in notes:
        add_event(conn, volume_id, "NOTE", detail=detail)


@pytest.mark.parametrize("status, notes", [
    # The burn of a BURNED volume was killed: it is still BURNING.
    ("BURNING", _RC7_NOTES_BURNED_THEN_BURNING),
    # The burn failed and the older LCSAS set it STAGING (#810).
    ("STAGING", (*_RC7_NOTES_BURNED_THEN_BURNING,
                 "Status changed: BURNING → STAGING")),
])
def test_burned_before_its_burn_is_never_deleted(staged, status, notes):
    orch, conn, result, manifest = staged
    _rc7_history(conn, manifest.volume_id, status, notes)

    assert VolumeLifecycle(conn).burn_evidence(manifest.volume_id) == _WAS_BURNED
    for call in (
        lambda: orch.abort_session(result.session_id),
        lambda: orch.abort_session(result.session_id, never_burned=True),
        lambda: orch.clean_session(result.session_id),
        lambda: orch.clean_session(result.session_id, force=True),
        lambda: orch.abort_volume(manifest.volume_label),
        lambda: orch.abort_volume(manifest.volume_label, never_burned=True),
    ):
        with pytest.raises(ValueError,
                           match=f"is {status}, but {_WAS_BURNED}"):
            call()
    vol = get_volume_by_label(conn, manifest.volume_label)
    assert vol is not None and vol.status == status
    assert get_pack_ids_for_volume(conn, manifest.volume_id)
    assert manifest.iso_path is not None and manifest.iso_path.exists()
    assert get_session(conn, result.session_id).status == "STAGED"


@pytest.mark.parametrize("notes", [
    # A staged volume whose burn was killed (#668).
    ("Status changed: STAGING → BURNING",),
])
def test_burning_from_staging_is_still_deleted(staged, notes):
    """Case 4 of #912: a volume that was never BURNED or VERIFIED."""
    orch, conn, result, manifest = staged
    _rc7_history(conn, manifest.volume_id, "BURNING", notes)

    assert VolumeLifecycle(conn).burn_evidence(manifest.volume_id) is None
    summary = orch.abort_session(result.session_id)
    assert summary.labels == [manifest.volume_label]
    assert get_volume_by_label(conn, manifest.volume_label) is None


# ---------------------------------------------------------------------
# #912: any status note out of BURNED or VERIFIED, ever, except the one a
# failing burn receipt wrote
# ---------------------------------------------------------------------

# rc1 to rc9 demoted a BURNED volume whose burn failed or was stopped:
# BURNED → BURNING → STAGING (#810).  A later burn from STAGING then
# makes the newest "→ BURNING" note say STAGING, so the #904 check no
# longer sees that the volume was BURNED.
_EARLIER_BURN_UNDONE = (*_RC7_NOTES_BURNED_THEN_BURNING,
                        "Status changed: BURNING → STAGING")
_WAS_BURNED_EARLIER = "it was BURNED when an earlier burn of it started"
_WAS_BURNED_THEN_STAGING = "it was BURNED before it was set to STAGING"


def _rc9_failing_receipt(name: str = "TEST-0001.receipt.json",
                         location: str = "Home",
                         ) -> tuple[tuple[str, str | None, str], ...]:
    """The events ``catalog import-receipts`` wrote for a receipt with
    ``verify_passed`` false for a BURNED volume with no disc recorded,
    from c5b3338 (rc8, rc9) until #810 (7935657, rc10), in this order, in
    one transaction: the status note (update_status), VERIFY_FAIL, then
    BURN_RECEIPT_IMPORTED with the receipt's provenance.  Each add_event
    stamped its own datetime.now(), so no two share a date."""
    return (
        ("NOTE", None, "Status changed: BURNED → STAGING"),
        ("VERIFY_FAIL", location, f"Offline burn receipt: {name}"),
        ("BURN_RECEIPT_IMPORTED", location, json.dumps({
            "iso_sha256": "c" * 64,
            "burn_date": "2026-11-12T09:30:00+00:00",
            "session_id": "",
            "device": "/dev/sr0",
            "pack_ids": [],
            "receipt_file": name,
            "verify_passed": False,
        })),
    )


def _note(detail: str) -> tuple[str, str | None, str]:
    return ("NOTE", None, detail)


def _old_history(conn, volume_id: int, status: str,
                 events: tuple[tuple[str, str | None, str], ...]) -> None:
    """Give the volume *status* and *events* (oldest first, each through
    add_event with its own timestamp), as an older LCSAS left it."""
    force_status(conn, volume_id, status)
    for event_type, location, detail in events:
        add_event(conn, volume_id, event_type, location=location,
                  detail=detail)


_KEPT = {
    # Case 1: the earlier burn from BURNED was undone, then a burn from
    # STAGING was killed (BURNING) or failed again (STAGING).
    "earlier-burn-undone, killed": (
        "BURNING",
        tuple(map(_note, (*_EARLIER_BURN_UNDONE,
                          "Status changed: STAGING → BURNING"))),
        _WAS_BURNED_EARLIER),
    "earlier-burn-undone, failed again": (
        "STAGING",
        tuple(map(_note, (*_EARLIER_BURN_UNDONE,
                          "Status changed: STAGING → BURNING",
                          "Status changed: BURNING → STAGING"))),
        _WAS_BURNED_EARLIER),
    # Case 2: "BURNED → STAGING".  Its only writer in rc1 to rc9 was
    # the receipt import above (c5b3338 to 7935657), with the failing
    # receipt beside it; the bare note counts as well.
    "burned-to-staging": (
        "STAGING", (_note("Status changed: BURNED → STAGING"),),
        _WAS_BURNED_THEN_STAGING),
    "burned-to-staging, killed": (
        "BURNING", (_note("Status changed: BURNED → STAGING"),
                    _note("Status changed: STAGING → BURNING")),
        _WAS_BURNED_THEN_STAGING),
    # Case 3, #483: the volume's only disc may have failed verification.
    # A catalog rebuild that read a disc of it marked it BURNED with raw
    # SQL and no note (rc8 db/rebuild.py _promote_read_discs); importing
    # a failing receipt then set it STAGING.  The receipt does not say
    # the BURNED status was wrong, so the volume is kept until removed
    # by hand (owner decision on #912), also after a later killed burn.
    "failing-receipt": (
        "STAGING", _rc9_failing_receipt(), _WAS_BURNED_THEN_STAGING),
    "failing-receipt, killed": (
        "BURNING", (*_rc9_failing_receipt(),
                    _note("Status changed: STAGING → BURNING")),
        _WAS_BURNED_THEN_STAGING),
}


@pytest.mark.parametrize("case", sorted(_KEPT))
def test_burned_earlier_is_never_deleted(staged, case):
    orch, conn, result, manifest = staged
    status, events, why = _KEPT[case]
    _old_history(conn, manifest.volume_id, status, events)

    for call in (
        lambda: orch.abort_session(result.session_id),
        lambda: orch.abort_session(result.session_id, never_burned=True),
        lambda: orch.clean_session(result.session_id),
        lambda: orch.clean_session(result.session_id, force=True),
        lambda: orch.abort_volume(manifest.volume_label),
        lambda: orch.abort_volume(manifest.volume_label, never_burned=True),
    ):
        with pytest.raises(ValueError, match=f"is {status}, but {why}"):
            call()
    assert VolumeLifecycle(conn).burn_evidence(manifest.volume_id) == why
    vol = get_volume_by_label(conn, manifest.volume_label)
    assert vol is not None and vol.status == status
    assert get_pack_ids_for_volume(conn, manifest.volume_id)
    assert manifest.iso_path is not None and manifest.iso_path.exists()
    assert get_session(conn, result.session_id).status == "STAGED"


@pytest.mark.parametrize("events, why", [
    # A failing receipt excepts no note: the newest note out of BURNED
    # gives the reason, here the receipt import's own.
    ((*map(_note, _EARLIER_BURN_UNDONE),
      _note("Status changed: STAGING → BURNING"),
      _note("Status changed: BURNING → BURNED"),
      *_rc9_failing_receipt()),
     _WAS_BURNED_THEN_STAGING),
    # A passing receipt is evidence by itself, and is read first.
    ((*_rc9_failing_receipt()[:2],
      ("BURN_RECEIPT_IMPORTED", "Home",
       json.dumps({"receipt_file": "TEST-0001.receipt.json",
                   "verify_passed": True}))),
     "a burn receipt of it was imported at Home"),
    # Out of VERIFIED counts too.
    ((_note("Status changed: VERIFIED → CONSOLIDATING"),),
     "it was VERIFIED before it was set to CONSOLIDATING"),
    # Notes that never leave BURNED or VERIFIED are no evidence.
    ((_note("Status changed: STAGING → BURNING"),
      _note("Status changed: BURNING → STAGING")),
     None),
])
def test_burn_evidence_from_status_notes(staged, events, why):
    _orch, conn, _result, manifest = staged
    _old_history(conn, manifest.volume_id, "STAGING", events)
    assert VolumeLifecycle(conn).burn_evidence(manifest.volume_id) == why


def test_mixed_session_deletes_nothing(tmp_path):
    """One never-burned volume and one rebuilt-style BURNED volume in the
    same burn session: the refusal comes before any delete."""
    config = _config(tmp_path)
    conn = get_connection(config.db_path)
    create_all(conn)
    register_repo(conn, "family", "Family",
                  str(config.repositories["family"].mirror_path))
    conn.commit()
    _add_packs(conn, config, 1, 3, 450_000)  # two volumes (#811)
    orch = _orchestrator(config, conn)
    result = orch.stage()
    first, second = result.manifests
    force_status(conn, second.volume_id, "BURNED")
    conn.commit()

    with pytest.raises(ValueError, match=f"{second.volume_label} is BURNED"):
        orch.abort_session(result.session_id)
    for m in (first, second):
        assert get_volume_by_label(conn, m.volume_label) is not None
        assert m.iso_path is not None and m.iso_path.exists()
    assert get_session(conn, result.session_id).status == "STAGED"
    conn.close()
