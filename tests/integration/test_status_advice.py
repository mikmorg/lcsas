"""#968 items 11 to 15: status and advice that agree with each other.

11. ``status`` during a running stage says a stage is running, not that
    the volume being made is INCOMPLETE and should be aborted.
12. After a stage killed hard, ``stage`` gives the advice ``status``
    gives: do not burn that session, abort it.
13. ``volume impact`` tells a volume whose packs were all pruned from the
    one whose packs all have another copy.
14. Read-only commands on a master newer than the code say so, once.
15. ``stage`` prints its "Staging packs ... complete" line once a volume.

Real rustic, real xorriso, every command through ``main()``.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import SESSION_MANIFEST
from lcsas.iso.xorriso import SubprocessXorrisoRunner
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _run(inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
         *argv: str) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = inst.lcsas(*argv)
    return rc, caplog.text


def _scanned(root: Path, repos: tuple[str, ...] = ("alpha",)) -> ScratchInstall:
    inst = ScratchInstall(root, repos=repos)
    inst.lcsas("location", "add", "Home_Shelf")
    for name in repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


# -- 11 --------------------------------------------------------------------

def test_status_during_a_running_stage_says_a_stage_is_running(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``status`` is run while the stage masters its first ISO: the volume
    row is committed, its ISO hash not yet recorded, and the stage holds
    the catalog lock, as a second terminal sees it."""
    inst = _scanned(tmp_path / "x")
    seen: dict[str, str] = {}
    real = SubprocessXorrisoRunner.create_iso

    def create_iso(self: SubprocessXorrisoRunner, *a: object,
                   **k: object) -> Path:
        if not seen:
            start = len(caplog.records)
            assert inst.lcsas("status") == 0
            seen["text"] = "\n".join(
                r.getMessage() for r in caplog.records[start:])
            capsys.readouterr()
            assert inst.lcsas("status", "--json") == 0
            seen["json"] = capsys.readouterr().out
        return real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(SubprocessXorrisoRunner, "create_iso", create_iso)
    with caplog.at_level(logging.INFO, logger="lcsas"):
        assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    (label,) = inst.labels()

    text = seen["text"]
    assert "INCOMPLETE" not in text, text
    assert "session abort" not in text, text
    assert "A stage is running" in text, text
    assert session in text and label in text, text

    doc = json.loads(seen["json"])
    assert doc["incomplete"] == {"labels": [], "sessions": []}
    assert doc["stage_running"]["session_id"] == session
    assert doc["stage_running"]["labels"] == [label]

    # Once the stage has finished, status is as before.
    rc, out = _run(inst, caplog, "status")
    assert rc == 0
    assert "A stage is running" not in out


# -- 12 --------------------------------------------------------------------

def test_stage_after_a_killed_stage_says_what_status_says(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A stage killed hard leaves no session.json (the stage writes it
    last); its finished volumes are on no disc and must not be burned."""
    inst = _scanned(tmp_path / "x")
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    ((staging_dir,),) = inst.query(
        "SELECT staging_dir FROM burn_sessions WHERE session_id = ?", session)
    (Path(staging_dir) / SESSION_MANIFEST).unlink()

    rc, status = _run(inst, caplog, "status")
    assert "Do not burn it" in status

    # Nothing new to stage: the refusal.
    rc, out = _run(inst, caplog, "stage")
    assert rc != 0
    assert "lcsas burn --session" not in out, out
    assert f"lcsas session abort {session}" in out, out
    assert "did not finish" in out, out

    # New packs: the newer snapshot needs the held-back packs too, so the
    # stage is refused (#946); every line gives status's advice.
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0
    rc, out = _run(inst, caplog, "stage")
    assert rc == 1, out
    assert "Burn them with" not in out, out
    assert "lcsas burn --session" not in out, out
    assert f"lcsas session abort {session}" in out, out
    assert "did not finish" in out, out


# -- 13 --------------------------------------------------------------------

def test_volume_impact_tells_pruned_from_copied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _scanned(tmp_path / "x")
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    inst.mark_burned(session)
    (label,) = inst.labels()
    (first,) = inst.mirror_snapshots("alpha")
    (inst.data("alpha") / "f1.bin").unlink()
    inst.backup("alpha", "f2.bin", size=60_000)
    inst.rustic("alpha", "forget", first)
    inst.rustic("alpha", "prune", "--max-unused", "0", "--instant-delete")
    assert inst.lcsas("scan") == 0
    ((pruned,),) = inst.query(
        "SELECT COUNT(*) FROM volume_packs vp JOIN packs p USING (pack_id) "
        "JOIN volumes v USING (volume_id) WHERE v.label = ? "
        "AND p.is_pruned = 1", label)
    assert pruned > 0

    rc, out = _run(inst, caplog, "volume", "impact", label)
    assert rc == 0, out
    assert "every pack on this volume is also on another live volume" \
        not in out, out
    assert f"{pruned} pruned pack(s)" in out, out

    rc, out = _run(inst, caplog, "copy", "destroy", label, "Home_Shelf")
    assert rc == 0, out
    assert f"DATA LOSS: {pruned} pack(s)" in out, out


# -- 14 --------------------------------------------------------------------

def test_read_only_commands_say_the_master_is_newer(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inst = _scanned(tmp_path / "x")
    conn = sqlite3.connect(inst.db)
    conn.execute("INSERT INTO schema_version (version) VALUES (11)")
    conn.commit()
    conn.close()
    for argv in (("status",), ("repo", "list"), ("session", "list")):
        rc, out = _run(inst, caplog, *argv)
        assert rc == 0, (argv, out)
        notes = [line for line in out.splitlines() if "v11" in line]
        assert len(notes) == 1, (argv, out)
        assert "newer" in notes[0]

    capsys.readouterr()
    assert inst.lcsas("status", "--json") == 0
    json.loads(capsys.readouterr().out)


# -- 15 --------------------------------------------------------------------

def test_stage_prints_its_complete_line_once_a_volume(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _scanned(tmp_path / "x", repos=("alpha", "beta"))
    rc, out = _run(inst, caplog, "stage")
    assert rc == 0, out
    assert len(inst.labels()) == 1
    lines = [line for line in out.splitlines()
             if "Staging packs:" in line and "complete" in line]
    assert len(lines) == 1, out
    ((n,),) = inst.query("SELECT COUNT(*) FROM volume_packs")
    assert f"{n}/{n} complete" in lines[0]
