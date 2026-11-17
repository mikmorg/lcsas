"""#862: the burn refuses a session whose stage was killed.

A stage that stops part-way re-finalises the volumes it keeps (#813), but
a hard kill (SIGKILL, power loss) runs no clean-up.  Killed while it
hard-linked the packs of volume k+1, the stage leaves volumes 1..k with
snapshot files whose packs are on no disc.  Their ISOs are finished (the
#665 check passes), so the burn now also refuses a session folder with
no session.json: the stage writes it last.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import (
    SESSION_MANIFEST,
    unfinished_stage_refusal,
    unfinished_stage_refusal_for_iso,
)
from lcsas.db.sessions import get_session_volumes
from lcsas.db.volumes import list_volumes
from lcsas.exceptions import BurnError
from tests.unit.test_stage_partial_snapshots import (
    ALL,
    FIRST,
    OLD,
    _disc_snapshots,
    _env,
)


class _Killed(BaseException):
    """Stands for SIGKILL: no handler of the stage runs."""


def _killed_stage(tmp_path, monkeypatch):
    """A three-volume stage killed while it hard-links volume 2's pack."""
    orch, conn, _x = _env(tmp_path, monkeypatch)
    # A hard kill runs no clean-up (#813's handler).
    monkeypatch.setattr(orch, "_after_failed_stage", lambda *a, **k: None)
    import lcsas.staging.builder as builder_mod

    real = builder_mod.hardlink_or_copy
    calls = {"n": 0}

    def _link(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise _Killed
        real(src, dst)

    monkeypatch.setattr(builder_mod, "hardlink_or_copy", _link)
    with pytest.raises(_Killed):
        orch.stage()
    monkeypatch.setattr(builder_mod, "hardlink_or_copy", real)
    [session_id] = [r[0] for r in conn.execute(
        "SELECT session_id FROM burn_sessions")]
    return orch, conn, session_id


def test_burn_refuses_a_session_whose_stage_was_killed(tmp_path, monkeypatch):
    orch, conn, session_id = _killed_stage(tmp_path, monkeypatch)
    [kept] = list_volumes(conn)
    [sv] = get_session_volumes(conn, session_id)
    # Volume 1 is finished: the #665 check does not refuse it ...
    assert sv.iso_sha256
    assert Path(sv.iso_path).is_file()
    # ... but it carries ALL, which needs volumes never staged.
    session_dir = Path(sv.iso_path).parent
    assert _disc_snapshots(session_dir / kept.label) == {OLD, FIRST, ALL}
    assert not (session_dir / SESSION_MANIFEST).exists()

    with pytest.raises(BurnError) as exc:
        orch.burn_session(session_id, location="Home_Shelf",
                          device="/dev/null", skip_burn=True)
    msg = str(exc.value)
    assert "did not finish" in msg
    assert SESSION_MANIFEST in msg
    assert f"lcsas session abort {session_id}" in msg
    assert f"lcsas stage --clean --session {session_id} --force" in msg
    assert "Nothing was burned" in msg
    assert [v.status for v in list_volumes(conn)] == ["STAGING"]
    assert conn.execute(
        "SELECT COUNT(*) FROM volume_copies").fetchone()[0] == 0

    # The command the message names removes the session.
    orch.clean_session(session_id, force=True)
    assert list_volumes(conn) == []
    assert not session_dir.exists()


def test_burn_iso_refuses_an_iso_in_a_killed_stage_folder(
    tmp_path, monkeypatch, caplog,
):
    from lcsas.cli.commands.burn import cmd_burn_iso

    _orch, conn, session_id = _killed_stage(tmp_path, monkeypatch)
    [sv] = get_session_volumes(conn, session_id)
    iso = Path(sv.iso_path)
    refusal = unfinished_stage_refusal_for_iso(iso)
    assert refusal is not None
    assert "lcsas session abort <session-id>" in refusal
    assert "'lcsas session list' shows its id" in refusal

    burned = []
    monkeypatch.setattr(
        "lcsas.iso.xorriso.SubprocessXorrisoRunner.burn_iso",
        lambda self, *a, **k: burned.append(a),
    )
    args = argparse.Namespace(
        iso_path=iso, emit_receipt=None, verify_only=False, location=None,
        verify=False, device="/dev/null", label=None, session=None,
        config=None,
    )
    assert cmd_burn_iso(args) == 1
    assert burned == []
    assert "did not finish" in caplog.text

    # A copy of the ISO on its own (the portable workflow) is not refused.
    alone = tmp_path / "elsewhere" / iso.name
    alone.parent.mkdir()
    alone.write_bytes(iso.read_bytes())
    assert unfinished_stage_refusal_for_iso(alone) is None


def test_a_finished_stage_and_a_cleaned_partial_stage_have_the_marker(
    tmp_path, monkeypatch,
):
    orch, _conn, _x = _env(tmp_path / "full", monkeypatch)
    result = orch.stage()
    assert (result.staging_dir / SESSION_MANIFEST).is_file()
    assert unfinished_stage_refusal(result.staging_dir,
                                    result.session_id) is None
    for m in result.manifests:
        assert m.iso_path is not None
        assert unfinished_stage_refusal_for_iso(m.iso_path) is None
    assert not list(result.staging_dir.glob("*.tmp"))

    # A stage that stopped and ran its clean-up (#813) is burnable.
    orch, conn, _x = _env(tmp_path / "partial", monkeypatch, fail_on=2)
    with pytest.raises(RuntimeError, match="xorriso failed"):
        orch.stage()
    [session_id] = [r[0] for r in conn.execute(
        "SELECT session_id FROM burn_sessions")]
    [sv] = get_session_volumes(conn, session_id)
    session_dir = Path(sv.iso_path).parent
    assert (session_dir / SESSION_MANIFEST).is_file()
    assert unfinished_stage_refusal(session_dir, session_id) is None
    orch._burn_preflight(session_id, [sv])  # does not raise


def test_no_staging_folder_is_left_to_the_iso_check(tmp_path):
    assert unfinished_stage_refusal(tmp_path / "gone", "s") is None
