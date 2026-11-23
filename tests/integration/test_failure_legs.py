"""The owner's failure legs with no end-to-end test yet (#961).

The pre-production review (2026-11-23) worked each leg by hand and it
held; these make each one a test that crosses the real tools:

- a ``stage`` killed hard (SIGKILL, a real process), then the advice it
  gives followed: nothing double-staged, orphaned or stuck;
- discs lost and destroyed, with one location and with two;
- a real ``rustic prune``, then scan, consolidate and restore.

The other legs have their own: rebuild then stage
(``test_stage_label_reserve.py``), a password change
(``test_password_change_after_split.py``) and a retired set
(``test_retired_set_on_later_discs.py``).

Real rustic, real xorriso, every owner command through ``main()`` (the
killed stage through ``python -m lcsas``), and the heir's side through
the real ``restore.sh`` of a META disc.
"""
from __future__ import annotations

import logging
import os
import signal
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcsas.burn.orchestrator import SESSION_MANIFEST
from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_SRC = str(Path(__file__).resolve().parents[2] / "src")


def _run(inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
         *argv: str) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = inst.lcsas(*argv)
    return rc, caplog.text


def _restored(inst: ScratchInstall, name: str, out: Path,
              labels: list[str]) -> None:
    """The heir restores set *name* from the discs *labels* alone, and
    gets the set's folder as it is now."""
    run = inst.heir_restore(name, out, labels=labels)
    assert run.returncode == 0, run.stdout + run.stderr
    got = {k.rsplit("/", 1)[-1]: v for k, v in tree_digest(out).items()
           if k.endswith(".bin")}
    assert got == tree_digest(inst.data(name)), (got, labels)


def _in_service(inst: ScratchInstall) -> dict[str, set[str]]:
    """Label -> the packs of each volume that is not DEPRECATED or
    DESTROYED."""
    rows = inst.query(
        "SELECT v.label, p.sha256 FROM volumes v "
        "JOIN volume_packs vp ON vp.volume_id = v.volume_id "
        "JOIN packs p ON p.pack_id = vp.pack_id "
        "WHERE v.status NOT IN ('DEPRECATED', 'DESTROYED')")
    out: dict[str, set[str]] = {}
    for label, sha in rows:
        out.setdefault(str(label), set()).add(str(sha))
    return out


def _mirror_packs(inst: ScratchInstall, name: str) -> set[str]:
    return {p.name for p in (inst.mirror(name) / "data").rglob("*")
            if p.is_file()}


# -- a stage killed hard ----------------------------------------------------

def test_stage_killed_hard_then_aborted_then_staged_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """SIGKILL a real ``stage`` once its second volume is in the catalog
    (a set that needs three or more discs).
    ``stage`` then refuses and says to abort the session; after the
    abort a new stage puts every pack on exactly one disc, numbered past
    the killed session's labels, no staging folder is left without a
    session, and the heir restores from the new discs."""
    from lcsas.db.label_mark import label_seq

    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for i in range(3):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0

    proc = subprocess.Popen(
        [sys.executable, "-m", "lcsas", "--config", str(inst.config),
         "stage"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        env={**os.environ, "PYTHONPATH": _SRC,
             "TMPDIR": str(inst.root / "tmp"),
             "HOME": str(inst.root / "tmp")},
    )
    assert proc.stdout is not None
    seen: list[str] = []
    try:
        # Killed once its second volume is in the catalog: the first
        # disc made, the second being made.
        for line in proc.stdout:
            seen.append(line)
            try:
                volumes = inst.query("SELECT COUNT(*) FROM volumes")[0][0]
            except sqlite3.OperationalError:
                continue        # the stage is writing; ask at the next line
            if volumes >= 2:
                proc.send_signal(signal.SIGKILL)
                break
        proc.wait(timeout=120)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == -signal.SIGKILL, "".join(seen)
    (killed,) = inst.sessions()
    killed_labels = inst.labels()
    assert len(killed_labels) >= 2, (killed_labels, "".join(seen))
    ((staging_dir,),) = inst.query(
        "SELECT staging_dir FROM burn_sessions WHERE session_id = ?", killed)
    assert not (Path(staging_dir) / SESSION_MANIFEST).exists(), (
        "the stage was killed before it finished")

    rc, out = _run(inst, caplog, "stage")
    assert rc != 0, out
    assert f"lcsas session abort {killed}" in out, out

    rc, out = _run(inst, caplog, "session", "abort", killed)
    assert rc == 0, out
    assert inst.labels() == []
    rc, out = _run(inst, caplog, "stage")
    assert rc == 0, out

    # Every pack of the set on exactly one disc; none twice, none left.
    discs = _in_service(inst)
    on_discs = [sha for packs in discs.values() for sha in packs]
    assert len(on_discs) == len(set(on_discs)), discs
    assert set(on_discs) == _mirror_packs(inst, "alpha")
    # No label of the killed session is issued again.
    last_killed = max(label_seq(lb) or 0 for lb in killed_labels)
    assert all((label_seq(lb) or 0) > last_killed for lb in discs), (
        killed_labels, sorted(discs))
    # No staging folder is left without a session that is not cleaned.
    live_dirs = {Path(str(r[0])).name for r in inst.query(
        "SELECT staging_dir FROM burn_sessions WHERE status != 'CLEANED'")}
    folders = {p.name for p in inst.staging.iterdir() if p.is_dir()}
    assert folders <= live_dirs, (folders, live_dirs)

    sessions = [s for s in inst.sessions() if s != killed]
    (session,) = sessions
    inst.mark_burned(session)
    _restored(inst, "alpha", tmp_path / "out", sorted(discs))


# -- lost and destroyed discs -----------------------------------------------

def test_destroyed_discs_with_two_locations(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A volume with a disc at each of two locations.  Destroying one
    disc keeps the volume; ``stage --for-location`` burns its packs
    again for that place.  Destroying the other disc too makes the
    volume lost, and the heir restores from the disc made again."""
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",),
                          locations=("Home_Shelf", "Offsite"))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (first,) = inst.sessions()
    inst.mark_burned(first)
    inst.mark_burned(first, "Offsite")
    (label,) = inst.labels()

    rc, out = _run(inst, caplog, "copy", "destroy", label, "Offsite")
    assert rc == 0, out
    assert label in _in_service(inst), "one disc left: not lost"
    rc, out = _run(inst, caplog, "stage", "--for-location", "Offsite")
    assert rc == 0, out
    (again,) = [s for s in inst.sessions() if s != first]
    inst.mark_burned(again, "Offsite")
    (offsite,) = [lb for lb in inst.labels() if lb != label]
    assert _in_service(inst)[offsite] == _in_service(inst)[label]

    rc, out = _run(inst, caplog, "copy", "destroy", label, "Home_Shelf")
    assert rc == 0, out
    ((status,),) = inst.query("SELECT status FROM volumes WHERE label = ?",
                              label)
    assert status == "DEPRECATED", "its last disc gone: the volume is lost"
    assert label not in _in_service(inst)
    _restored(inst, "alpha", tmp_path / "out", [offsite])


def test_lost_disc_with_one_location_is_staged_again(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """One location: the only disc of a volume destroyed.  The volume is
    lost, ``stage`` puts its packs on a new disc, and the heir restores
    from the new disc alone."""
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (first,) = inst.sessions()
    inst.mark_burned(first)
    (label,) = inst.labels()

    rc, out = _run(inst, caplog, "copy", "destroy", label, "Home_Shelf")
    assert rc == 0, out
    assert label not in _in_service(inst), "its only disc gone: lost"
    rc, out = _run(inst, caplog, "stage")
    assert rc == 0, out
    (again,) = [s for s in inst.sessions() if s != first]
    inst.mark_burned(again)
    (new,) = [lb for lb in inst.labels() if lb != label]
    assert _in_service(inst)[new] == _mirror_packs(inst, "alpha")
    _restored(inst, "alpha", tmp_path / "out", [new])


# -- prune, then scan, consolidate and restore --------------------------------

def test_prune_then_scan_consolidate_and_restore(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first backup forgotten and pruned by real rustic.  ``scan``
    records its packs as pruned; ``consolidate`` of both discs leaves
    them behind and keeps every live pack; the sources are deprecated;
    the heir restores the set from the consolidated disc alone."""
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.backup("alpha", "f1.bin", size=60_000)
    (snap1,) = inst.mirror_snapshots("alpha")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (s1,) = inst.sessions()
    inst.mark_burned(s1)
    first_packs = _mirror_packs(inst, "alpha")

    (inst.data("alpha") / "f1.bin").unlink()
    inst.backup("alpha", "f2.bin", size=60_000)
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (s2,) = [s for s in inst.sessions() if s != s1]
    inst.mark_burned(s2)
    sources = inst.labels()
    assert len(sources) == 2

    inst.rustic("alpha", "forget", snap1)
    inst.rustic("alpha", "prune", "--max-unused", "0", "--instant-delete")
    live = _mirror_packs(inst, "alpha")
    pruned = first_packs - live
    assert pruned, "the prune should delete the first backup's packs"
    rc, out = _run(inst, caplog, "scan")
    assert rc == 0, out
    assert "damaged" not in out
    assert {str(r[0]) for r in inst.query(
        "SELECT sha256 FROM packs WHERE is_pruned = 1")} == pruned

    monkeypatch.setattr("lcsas.cli.commands.consolidate._confirm",
                        lambda *_a, **_k: True)
    rc, out = _run(inst, caplog, "consolidate", *sources, "--media-type",
                   "TEST_TINY", "--execute")
    assert rc == 0, out
    (s3,) = [s for s in inst.sessions() if s not in (s1, s2)]
    inst.mark_burned(s3)
    (merged,) = [lb for lb in inst.labels() if lb not in sources]
    assert _in_service(inst)[merged] == live

    rc, out = _run(inst, caplog, "consolidate", *sources, "--media-type",
                   "TEST_TINY", "--deprecate")
    assert rc == 0, out
    assert set(_in_service(inst)) == {merged}
    disc_packs = {p.name for p in
                  (inst.disc_trees()[merged] / "data").rglob("*")
                  if p.is_file()}
    assert disc_packs == live and not disc_packs & pruned
    _restored(inst, "alpha", tmp_path / "out", [merged])
