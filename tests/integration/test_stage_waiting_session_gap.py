"""#946: no stage may leave the newest snapshots off the newest disc.

A session staged while an earlier one waited to be burned left out every
snapshot that needed the waiting session's packs.  Once both were burned
the highest-labelled disc lacked the newest snapshot of a set, nothing
ever staged it again, and the heir's ``restore.sh`` restored an older
backup with "restore complete", exit 0.

``stage`` now refuses the stage that would create that gap, on every path
(plain, ``--for-location``, consolidation), and names the waiting
session.  Stages that do not create a gap (a second location while the
first location's session waits; two collections that each hold every
repository) still run.

Real rustic, real xorriso, every owner command through ``main()``, and
the heir's side through the real ``restore.sh`` of a META disc.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lcsas.exceptions import StagingError
from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _first_disc_burned(root: Path, **kwargs: object) -> ScratchInstall:
    """alpha and beta backed up once; their first disc staged and burned."""
    inst = ScratchInstall(root, **kwargs)  # type: ignore[arg-type]
    for name in inst.repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    inst.mark_burned(session)
    return inst


def _second_backup(inst: ScratchInstall, *names: str) -> None:
    for name in names or inst.repos:
        inst.backup(name, "r4.bin")
    assert inst.lcsas("scan") == 0


def _assert_refused(
    inst: ScratchInstall, caplog: pytest.LogCaptureFixture, waiting: str,
    *argv: str,
) -> None:
    before = inst.snapshot()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", *argv) == 1
    assert f"Session {waiting}" in caplog.text
    assert "waits to be burned" in caplog.text
    assert f"lcsas session abort {waiting}" in caplog.text
    assert "Unexpected error" not in caplog.text
    assert inst.snapshot() == before, "a refused stage changed something"


def test_case_a_one_repository_at_a_time_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _first_disc_burned(tmp_path / "x")
    _second_backup(inst)
    assert inst.lcsas("stage", "--repo", "alpha") == 0
    s1 = inst.sessions()[-1]
    _assert_refused(inst, caplog, s1, "--repo", "beta")
    assert "snapshot(s) of 'alpha'" in caplog.text


def test_case_b_one_location_twice_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _first_disc_burned(tmp_path / "x")
    _second_backup(inst)
    assert inst.lcsas("stage", "--for-location", "Home_Shelf") == 0
    s1 = inst.sessions()[-1]
    inst.backup("alpha", "r5.bin")
    assert inst.lcsas("scan") == 0
    _assert_refused(inst, caplog, s1, "--for-location", "Home_Shelf")


def test_interrupted_stage_then_stage_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volume 1 of 3 kept by a Ctrl-C; staging again would leave the
    newest snapshot off the new discs."""
    from lcsas.burn import orchestrator as orch_mod

    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for i in range(3):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    real = orch_mod.BurnOrchestrator._stage_single_volume
    calls = {"n": 0}

    def stop_after_first(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(self, *args, **kwargs)

    monkeypatch.setattr(orch_mod.BurnOrchestrator, "_stage_single_volume",
                        stop_after_first)
    # #968: main() ends a Ctrl-C quietly with 130.
    assert inst.lcsas("stage") == 130
    monkeypatch.undo()
    assert len(inst.labels()) == 1
    (s1,) = inst.sessions()
    _assert_refused(inst, caplog, s1)


def test_consolidation_while_a_session_waits_is_refused(
    tmp_path: Path,
) -> None:
    """Consolidation stages through the same stage(): its disc would be
    the newest and lack the waiting session's snapshots."""
    inst = _first_disc_burned(tmp_path / "x")
    _second_backup(inst)
    assert inst.lcsas("stage") == 0
    first_disc_packs = [str(r[0]) for r in inst.query(
        "SELECT p.sha256 FROM packs p JOIN volume_packs vp USING (pack_id) "
        "JOIN volumes v USING (volume_id) WHERE v.label = ?",
        inst.labels()[0])]
    before = inst.snapshot()
    with inst.orchestrator() as orch, pytest.raises(
            StagingError, match="waits to be burned"):
        orch.stage(pack_sha256s=first_disc_packs)
    assert inst.snapshot() == before


def test_second_location_while_first_waits_still_stages(
    tmp_path: Path,
) -> None:
    """Not a gap: the second location's session holds every pack."""
    inst = _first_disc_burned(tmp_path / "x",
                              locations=("Home_Shelf", "Bank_Box"))
    _second_backup(inst)
    assert inst.lcsas("stage", "--for-location", "Home_Shelf") == 0
    assert inst.lcsas("stage", "--for-location", "Bank_Box") == 0
    newest = inst.labels()[-1]
    for name in inst.repos:
        assert inst.disc_snapshots(newest, name) == inst.mirror_snapshots(name)


def test_two_collections_each_holding_every_repository_still_stage(
    tmp_path: Path,
) -> None:
    """The owner's flow: one collection per location, each with every
    repository, staged back to back before either is burned."""
    collections = (
        "[collections.home]\n"
        'location = "Home_Shelf"\n'
        'repos = ["alpha", "beta"]\n'
        "[collections.bank]\n"
        'location = "Bank_Box"\n'
        'repos = ["alpha", "beta"]\n'
    )
    inst = _first_disc_burned(tmp_path / "x", extra_config=collections,
                              locations=("Home_Shelf", "Bank_Box"))
    _second_backup(inst)
    assert inst.lcsas("stage", "--collection", "home") == 0
    assert inst.lcsas("stage", "--collection", "bank") == 0
    newest = inst.labels()[-1]
    for name in inst.repos:
        assert inst.disc_snapshots(newest, name) == inst.mirror_snapshots(name)


def test_burn_then_stage_gives_the_heir_the_newest_backup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """Case A, refused, then the refusal's way out: burn the waiting
    session, stage again.  The newest disc carries every snapshot and the
    heir's restore.sh restores the newest backup byte for byte."""
    inst = _first_disc_burned(tmp_path / "x")
    _second_backup(inst)
    assert inst.lcsas("stage", "--repo", "alpha") == 0
    s1 = inst.sessions()[-1]
    _assert_refused(inst, caplog, s1, "--repo", "beta")
    inst.mark_burned(s1)
    assert inst.lcsas("stage", "--repo", "beta") == 0
    inst.mark_burned(inst.sessions()[-1])

    newest = inst.labels()[-1]
    for name in inst.repos:
        assert inst.disc_snapshots(newest, name) == inst.mirror_snapshots(name)

    out = tmp_path / "restored"
    run = inst.heir_restore("alpha", out)
    assert run.returncode == 0, run.stdout + run.stderr
    restored = tree_digest(out)
    expected = tree_digest(inst.data("alpha"))
    assert {k.rsplit("/", 1)[-1]: v for k, v in restored.items()
            if k.endswith(".bin")} == expected, (restored, expected)
