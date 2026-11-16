"""``burn --session`` keeps a burned volume burned (#810) and skips the
volumes that already have a verified disc at the location (#814).

#810: a BURNED volume with no disc record (a catalog rebuilt from discs)
used to go BURNED -> BURNING -> STAGING when its burn failed, after which
``session abort`` and ``catalog reconcile --fix`` deleted it although a
disc of it exists.  It now stays BURNED for the whole burn: VERIFIED when
the new disc passes, BURNED otherwise.  A STAGING volume still goes back
to STAGING.  A BURNING volume an older LCSAS made from a BURNED one goes
back to BURNED.

#814: re-running a PARTIAL burn session burns only the volumes with no
verified disc at that location, unless ``reburn``.

The catalog is a real file; only xorriso and the device reader are faked.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from lcsas.burn.orchestrator import BurnOrchestrator
from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.locations import ensure_location
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.sessions import get_session
from lcsas.db.volume_copies import get_copies_for_volume
from lcsas.db.volume_events import get_events_for_volume
from lcsas.db.volumes import (
    get_volume_by_id,
    status_before_burning,
    update_status,
)
from tests.unit.test_session_pipeline import (
    _make_config,
    _seed_packs,
    _wire_drive_identity,
)


class _RecordingPrompt:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def checkpoint(self, message: str) -> None:
        self.messages.append(message)


def _setup(tmp_path, monkeypatch, *, num_packs: int, pack_size: int):
    monkeypatch.setattr("lcsas.burn.orchestrator.eject_tray", lambda _d: True)
    config = _make_config(tmp_path, num_repos=1)
    conn = get_connection(config.db_path)
    create_all(conn)
    for name in config.repositories:
        register_repo(conn, name, name.title(),
                      str(config.repositories[name].mirror_path))
    _seed_packs(conn, config, num_packs=num_packs, pack_size=pack_size)
    conn.commit()

    xorriso = MagicMock()

    def _fake_create_iso(source_dir, output_iso, volume_label, **kwargs):
        Path(output_iso).write_bytes(b"\x00" * 1024)
        return Path(output_iso)

    xorriso.create_iso.side_effect = _fake_create_iso
    _wire_drive_identity(xorriso)
    # The disc reads back as the all-zeros ISO unless a test says that the
    # disc of a volume is bad (labels in env["bad"]).
    bad: set[str] = set()

    def _reader(device: str, n: int) -> str:
        burned = Path(xorriso.burn_iso.call_args[0][0]).stem
        data = b"\x01" if burned in bad else b"\x00"
        return hashlib.sha256(data * n).hexdigest()

    prompt = _RecordingPrompt()
    orch = BurnOrchestrator(config, conn, xorriso, MagicMock(),
                            device_reader=_reader, prompt=prompt)
    ensure_location(conn, "Home")
    ensure_location(conn, "Bank")
    result = orch.stage()
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{config.mirror_base_path}"\n'
        f'staging = "{config.staging_path}"\n'
        f'database = "{config.db_path}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
    )
    return {
        "orch": orch, "conn": conn, "xorriso": xorriso, "prompt": prompt,
        "bad": bad, "cfg": str(cfg), "db": str(config.db_path),
        "session_id": result.session_id,
        "manifests": result.manifests,
    }


@pytest.fixture
def one(tmp_path, monkeypatch):
    """One staged volume of one burn session."""
    env = _setup(tmp_path, monkeypatch, num_packs=3, pack_size=50)
    [manifest] = env["manifests"]
    env["volume_id"] = manifest.volume_id
    env["label"] = manifest.volume_label
    yield env
    env["conn"].close()


@pytest.fixture
def two(tmp_path, monkeypatch):
    """Two staged volumes of one burn session (TEST_TINY is 2 MB)."""
    # 450 KB packs: two fit a TEST_TINY volume beside its disc metadata
    # (#811), three do not.
    env = _setup(tmp_path, monkeypatch, num_packs=3, pack_size=450_000)
    assert len(env["manifests"]) == 2
    yield env
    env["conn"].close()


def _burn(env, location: str = "Home", **kwargs):
    return env["orch"].burn_session(env["session_id"], location=location,
                                    device="/dev/sr0", interactive=True,
                                    **kwargs)


def _status(env, volume_id: int | None = None) -> str:
    return get_volume_by_id(env["conn"],
                            volume_id or env["volume_id"]).status


def _make_burned(env) -> None:
    """What 'catalog rebuild' writes for a volume whose own disc it read:
    BURNED, no disc record (db/rebuild.py _promote_read_discs)."""
    env["conn"].execute(
        "UPDATE volumes SET status = 'BURNED', "
        "created_at = datetime('now', '-48 hours') WHERE volume_id = ?",
        (env["volume_id"],),
    )
    env["conn"].commit()


def _burning_notes(env) -> list[str]:
    return [
        e.detail for e in get_events_for_volume(env["conn"], env["volume_id"],
                                                "NOTE")
        if e.detail.endswith("→ BURNING")
    ]


def _assert_abort_and_reconcile_keep_it(env, caplog) -> None:
    """``session abort`` refuses, ``reconcile --fix`` keeps the volume."""
    caplog.clear()
    assert main(["--config", env["cfg"], "session", "abort",
                 env["session_id"]]) == 1
    assert "BURNED, but no disc of it is recorded" in caplog.text
    for extra in ([], ["--include-waiting"]):
        assert main(["--db", env["db"], "catalog", "reconcile", "--fix",
                     "--yes", *extra]) == 0, caplog.text
    conn = get_connection(Path(env["db"]))
    try:
        row = conn.execute("SELECT status FROM volumes WHERE volume_id = ?",
                           (env["volume_id"],)).fetchone()
    finally:
        conn.close()
    assert row is not None and row[0] == "BURNED"
    assert get_session(env["conn"], env["session_id"]).status != "CLEANED"


# ---------------------------------------------------------------------
# #810: a BURNED volume stays BURNED
# ---------------------------------------------------------------------


def test_burned_volume_failed_verify_stays_burned(one, caplog):
    _make_burned(one)
    one["bad"].add(one["label"])

    [receipt] = _burn(one)

    assert not receipt.verify_passed
    assert _status(one) == "BURNED"
    assert get_copies_for_volume(one["conn"], one["volume_id"]) == []
    assert _burning_notes(one) == []
    events = get_events_for_volume(one["conn"], one["volume_id"])
    assert any(e.event_type == "VERIFY_FAIL" for e in events)
    # VERIFY_FAIL_REBURN says an earlier disc was recorded; none is.
    assert not any(e.event_type == "VERIFY_FAIL_REBURN" for e in events)
    _assert_abort_and_reconcile_keep_it(one, caplog)


@pytest.mark.parametrize("exc", [RuntimeError("drive error"),
                                 KeyboardInterrupt()])
def test_burned_volume_burn_error_or_ctrl_c_stays_burned(one, caplog, exc):
    _make_burned(one)
    one["xorriso"].burn_iso.side_effect = exc

    with pytest.raises(type(exc)):
        _burn(one)

    assert _status(one) == "BURNED"
    assert _burning_notes(one) == []
    _assert_abort_and_reconcile_keep_it(one, caplog)


def test_burned_volume_passing_disc_becomes_verified(one):
    _make_burned(one)

    [receipt] = _burn(one)

    assert receipt.verify_passed
    vol = get_volume_by_id(one["conn"], one["volume_id"])
    assert vol.status == "VERIFIED"
    assert vol.closed_at is not None
    [copy] = get_copies_for_volume(one["conn"], one["volume_id"])
    assert copy.location == "Home" and copy.last_verified_at is not None
    assert _burning_notes(one) == []


def test_burned_volume_cannot_go_back_to_staging_or_burning(one):
    _make_burned(one)
    for target in ("STAGING", "BURNING"):
        with pytest.raises(ValueError, match="Invalid status transition"):
            update_status(one["conn"], one["volume_id"], target)
    assert _status(one) == "BURNED"


# --- an interrupted burn (the BURNING recovery path of #668) ---------


def _left_burning_by_older_lcsas(env) -> None:
    """An older LCSAS moved a BURNED volume to BURNING for its burn, then
    the burn was killed outright: the volume is BURNING, and the NOTE
    update_status wrote records where it came from."""
    _make_burned(env)
    update_status(env["conn"], env["volume_id"], "BURNING", force=True)


def test_status_before_burning(one):
    conn, vid = one["conn"], one["volume_id"]
    assert status_before_burning(conn, vid) == "STAGING"  # no note
    update_status(conn, vid, "BURNING")
    assert status_before_burning(conn, vid) == "STAGING"
    update_status(conn, vid, "STAGING")
    conn.execute("UPDATE volumes SET status = 'BURNED' WHERE volume_id = ?",
                 (vid,))
    update_status(conn, vid, "BURNING", force=True)
    assert status_before_burning(conn, vid) == "BURNED"


def test_interrupted_burn_of_burned_volume_goes_back_to_burned(one, caplog):
    _left_burning_by_older_lcsas(one)
    one["xorriso"].burn_iso.side_effect = RuntimeError("no disc")

    with pytest.raises(RuntimeError):
        _burn(one)

    assert _status(one) == "BURNED"
    assert "resetting it to BURNED" in caplog.text
    _assert_abort_and_reconcile_keep_it(one, caplog)


def test_interrupted_burn_of_burned_volume_then_passing_disc(one):
    _left_burning_by_older_lcsas(one)

    [receipt] = _burn(one)

    assert receipt.verify_passed
    assert _status(one) == "VERIFIED"


# --- a STAGING volume still goes back to STAGING ---------------------


@pytest.mark.parametrize("how", ["verify_fail", "error", "ctrl_c",
                                 "hard_kill"])
def test_staging_volume_still_returns_to_staging(one, how):
    if how == "verify_fail":
        one["bad"].add(one["label"])
        [receipt] = _burn(one)
        assert not receipt.verify_passed
    elif how == "hard_kill":
        update_status(one["conn"], one["volume_id"], "BURNING")
        one["xorriso"].burn_iso.side_effect = RuntimeError("no disc")
        with pytest.raises(RuntimeError):
            _burn(one)
    else:
        exc = RuntimeError("x") if how == "error" else KeyboardInterrupt()
        one["xorriso"].burn_iso.side_effect = exc
        with pytest.raises(type(exc)):
            _burn(one)
    assert _status(one) == "STAGING"


# ---------------------------------------------------------------------
# #814: a re-run burns only what still needs a disc at the location
# ---------------------------------------------------------------------


def test_rerun_of_partial_session_burns_only_the_failed_volume(two):
    first, second = two["manifests"]
    two["bad"].add(second.volume_label)
    receipts = _burn(two)
    assert [r.verify_passed for r in receipts] == [True, False]
    assert get_session(two["conn"], two["session_id"]).status == "PARTIAL"

    two["bad"].clear()
    two["xorriso"].burn_iso.reset_mock()
    two["prompt"].messages.clear()
    [receipt] = _burn(two)

    assert receipt.volume_label == second.volume_label
    assert receipt.verify_passed
    [call] = two["xorriso"].burn_iso.call_args_list
    assert Path(call[0][0]).stem == second.volume_label
    assert any("Disc 1 of 1" in m for m in two["prompt"].messages)
    assert get_session(two["conn"], two["session_id"]).status == "COMPLETE"
    for m in (first, second):
        assert _status(two, m.volume_id) == "VERIFIED"
        assert len(get_copies_for_volume(two["conn"], m.volume_id)) == 1


def test_nothing_to_burn_when_every_volume_has_a_verified_disc(two, caplog):
    _burn(two)
    two["xorriso"].burn_iso.reset_mock()
    two["xorriso"].media_status.reset_mock()
    two["prompt"].messages.clear()

    assert _burn(two) == []
    assert two["prompt"].messages == []

    two["xorriso"].burn_iso.assert_not_called()
    two["xorriso"].media_status.assert_not_called()
    assert "Nothing to burn" in caplog.text
    assert get_session(two["conn"], two["session_id"]).status == "COMPLETE"


def test_reburn_burns_them_again_and_keeps_one_record_per_location(two):
    _burn(two)
    before = {
        m.volume_id: get_copies_for_volume(two["conn"], m.volume_id)[0]
        for m in two["manifests"]
    }
    two["xorriso"].burn_iso.reset_mock()

    receipts = _burn(two, reburn=True)

    assert len(receipts) == 2 and all(r.verify_passed for r in receipts)
    assert two["xorriso"].burn_iso.call_count == 2
    for m in two["manifests"]:
        [copy] = get_copies_for_volume(two["conn"], m.volume_id)
        assert copy.id == before[m.volume_id].id
        assert copy.burn_date != before[m.volume_id].burn_date


def test_another_location_burns_every_volume(two):
    _burn(two)
    receipts = _burn(two, location="Bank")
    assert len(receipts) == 2
    for m in two["manifests"]:
        locs = {c.location for c in get_copies_for_volume(two["conn"],
                                                          m.volume_id)}
        assert locs == {"Home", "Bank"}


def test_an_unverified_or_retired_disc_does_not_count(two):
    first, second = two["manifests"]
    _burn(two)
    conn = two["conn"]
    conn.execute("UPDATE volume_copies SET last_verified_at = NULL "
                 "WHERE volume_id = ?", (first.volume_id,))
    conn.execute("UPDATE volume_copies SET status = 'DESTROYED' "
                 "WHERE volume_id = ?", (second.volume_id,))
    conn.commit()
    two["xorriso"].burn_iso.reset_mock()

    # The retired disc was the volume's only one, so it is lost now;
    # the burn puts it back in service (#726).
    receipts = _burn(two)
    assert sorted(r.volume_label for r in receipts) == sorted(
        [first.volume_label, second.volume_label])


def test_cli_reburn_flag_and_dry_run(two, caplog, monkeypatch):
    first, second = two["manifests"]
    two["bad"].add(second.volume_label)
    _burn(two)

    caplog.clear()
    assert main(["--config", two["cfg"], "burn", "--session",
                 two["session_id"], "--location", "Home", "--dry-run"]) == 0
    assert (f"  {first.volume_label}  status=VERIFIED  (verified disc at "
            f"Home: skipped)") in caplog.messages
    assert f"  {second.volume_label}  status=STAGING" in caplog.messages

    seen: dict[str, object] = {}

    def _fake_burn_session(self, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(BurnOrchestrator, "burn_session", _fake_burn_session)
    for extra, expected in (([], False), (["--reburn"], True)):
        # Any existing path passes the device check.
        assert main(["--config", two["cfg"], "burn", "--session",
                     two["session_id"], "--location", "Home",
                     "--device", two["db"], *extra]) == 0, caplog.text
        assert seen["reburn"] is expected


def test_reburn_hint_names_reburn_where_a_verified_disc_is_recorded(two):
    """The 'burn it again' hint (a failed receipt, 'lcsas status') adds
    --reburn where the burn would otherwise skip the volume."""
    from lcsas.cli.common import _reburn_hint

    _burn(two)
    vid = two["manifests"][0].volume_id
    sid = two["session_id"]
    assert _reburn_hint(two["conn"], vid, "Home").endswith(
        f"'lcsas burn --session {sid} --location Home --reburn'.")
    assert _reburn_hint(two["conn"], vid, "Bank").endswith(
        f"'lcsas burn --session {sid} --location Bank'.")
