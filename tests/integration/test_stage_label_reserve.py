"""#957: a burn session reserves its label numbers before its first disc.

Each disc's catalog is copied when its own volume is staged, so in a
session of n discs disc k used to carry a label mark of k.  A master
catalog rebuilt without the session's last disc then issued that disc's
number again: two different discs with one label.  ``stage`` now raises
the mark to the session's last number before the first disc is mastered,
so every disc of the session knows where numbering continues.

Real rustic, real xorriso, every owner command through ``main()``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from lcsas.db.label_mark import label_seq
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _three_disc_install(root: Path) -> ScratchInstall:
    """One set whose packs need three TEST_TINY discs."""
    inst = ScratchInstall(root, repos=("alpha",))
    for i in range(3):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def test_every_disc_of_a_session_carries_its_last_number(
    tmp_path: Path,
) -> None:
    inst = _three_disc_install(tmp_path / "x")
    assert inst.lcsas("stage") == 0
    labels = inst.labels()
    assert len(labels) >= 3, labels
    last = max(label_seq(lb) or 0 for lb in labels)
    marks = {lb: inst.disc_user_version(lb) for lb in labels}
    assert set(marks.values()) == {last}, marks


def test_rebuild_from_first_disc_does_not_reissue_a_label(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _three_disc_install(tmp_path / "x")
    assert inst.lcsas("stage") == 0
    (session,) = inst.sessions()
    inst.mark_burned(session)
    labels = inst.labels()
    first = labels[0]

    # The master catalog is lost; only the session's first disc is at hand.
    inst.db.unlink()
    for side in inst.db.parent.glob("catalog.db-*"):
        side.unlink()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("catalog", "rebuild", "--output", str(inst.db),
                          str(inst.disc_trees()[first])) == 0
    rebuild_log = caplog.text

    inst.backup("alpha", "more.bin", size=5000)
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage", "--repo", "alpha") == 0
    new = sorted(set(inst.labels()) - set(labels[:1]))
    # Volumes 2..n were not read, so they are not in the rebuilt catalog;
    # whatever was staged now must be numbered past the session's last.
    assert new, inst.labels()
    assert all((label_seq(lb) or 0) > (label_seq(labels[-1]) or 0)
               for lb in new), (labels, new)
    # The rebuild said where numbering continues.
    last = label_seq(labels[-1]) or 0
    assert f"Labels continue after number {last:04d}," in rebuild_log


def test_interrupted_session_numbers_are_not_reused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Volume 1 kept, volumes 2 and 3 never made: their numbers stay
    reserved."""
    from lcsas.burn import orchestrator as orch_mod

    inst = _three_disc_install(tmp_path / "x")
    real = orch_mod.BurnOrchestrator._stage_single_volume
    calls = {"n": 0}

    def stop_after_first(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] == 2:
            raise KeyboardInterrupt
        return real(self, *args, **kwargs)

    monkeypatch.setattr(orch_mod.BurnOrchestrator, "_stage_single_volume",
                        stop_after_first)
    with pytest.raises(KeyboardInterrupt):
        inst.lcsas("stage")
    monkeypatch.undo()
    kept = inst.labels()
    assert len(kept) == 1, kept
    planned_last = label_seq(kept[0])
    assert planned_last is not None
    # The kept disc knows the session planned three numbers.
    assert inst.disc_user_version(kept[0]) >= planned_last + 2

    (session,) = inst.sessions()
    assert inst.lcsas("session", "abort", session) == 0
    assert inst.lcsas("stage") == 0
    new = inst.labels()
    assert all((label_seq(lb) or 0) > planned_last + 2 for lb in new), new
