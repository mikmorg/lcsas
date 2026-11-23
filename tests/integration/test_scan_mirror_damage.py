"""#947: a pack file lost from the mirror is damage, not a prune.

``lcsas scan`` used to record any pack missing from a mirror's ``data/``
as pruned, with no warning under the bulk threshold.  When the lost file
held a tree, the next ``stage`` left every snapshot of that set off the
new disc ("packs not on any disc yet"), and the heir, holding every
disc, got "snapshot not found: latest".

A missing pack file is damage when the mirror's index still lists it as
a live pack; it is pruned when the index no longer lists it or lists it
"to delete" (rustic's two-step prune).  Damage is reported with the
discs that hold the pack, ``scan`` exits non-zero and the pack stays
active, so ``stage`` and consolidation refuse rather than drop it.

Real rustic, real xorriso, every owner command through ``main()``.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from lcsas.exceptions import StagingError
from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _burned_install(root: Path) -> ScratchInstall:
    inst = ScratchInstall(root)
    for name in inst.repos:
        inst.backup(name, "f1.bin", size=60_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    inst.mark_burned(inst.sessions()[-1])
    return inst


def _pack_files(inst: ScratchInstall, name: str) -> list[Path]:
    """The set's pack files, smallest first (the tree pack of a one-file
    backup is far smaller than its data pack)."""
    return sorted((p for p in (inst.mirror(name) / "data").rglob("*")
                   if p.is_file()), key=lambda p: p.stat().st_size)


def _is_pruned(inst: ScratchInstall, sha: str) -> int:
    (row,) = inst.query("SELECT is_pruned FROM packs WHERE sha256 = ?", sha)
    return int(row[0])


@pytest.mark.parametrize("which", ["tree", "data"])
def test_scan_reports_a_lost_pack_as_damage(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, which: str,
) -> None:
    inst = _burned_install(tmp_path / "x")
    tree, data = _pack_files(inst, "alpha")
    lost = tree if which == "tree" else data
    lost.unlink()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") != 0
    assert "Mirror of 'alpha' is damaged" in caplog.text
    assert lost.name in caplog.text
    assert inst.labels()[0] in caplog.text
    assert _is_pruned(inst, lost.name) == 0


def test_real_prune_is_still_recorded_as_pruned(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """rustic's two-step prune, and an instant prune: never damage."""
    inst = _burned_install(tmp_path / "x")
    (first,) = inst.mirror_snapshots("alpha")
    before = {p.name for p in _pack_files(inst, "alpha")}
    (inst.data("alpha") / "f1.bin").unlink()
    inst.backup("alpha", "f2.bin", size=60_000)
    inst.rustic("alpha", "forget", first)

    # Step 1: the packs are marked "to delete" in the index; files stay.
    inst.rustic("alpha", "prune", "--max-unused", "0")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert "damaged" not in caplog.text

    # Step 2: the files go.
    inst.rustic("alpha", "prune", "--max-unused", "0", "--keep-delete", "0s")
    gone = before - {p.name for p in _pack_files(inst, "alpha")}
    assert gone, "the second prune should delete the first backup's packs"
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert "damaged" not in caplog.text
    assert all(_is_pruned(inst, sha) == 1 for sha in gone)

    # An instant prune of the second backup (forgotten too).
    (second,) = inst.mirror_snapshots("alpha")
    (inst.data("alpha") / "f2.bin").unlink()
    inst.backup("alpha", "f3.bin", size=60_000)
    inst.rustic("alpha", "forget", second)
    live = {p.name for p in _pack_files(inst, "alpha")}
    inst.rustic("alpha", "prune", "--max-unused", "0", "--instant-delete")
    gone = live - {p.name for p in _pack_files(inst, "alpha")}
    assert gone
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert "damaged" not in caplog.text
    assert all(_is_pruned(inst, sha) == 1 for sha in gone)


def test_stage_after_damage_is_refused_then_repaired_from_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _burned_install(tmp_path / "x")
    tree, _data = _pack_files(inst, "alpha")
    disc = inst.disc_trees()[inst.labels()[0]]
    tree.unlink()
    assert inst.lcsas("scan") != 0

    # A new backup of beta only, as in the review's run.
    inst.backup("beta", "f2.bin")
    assert inst.lcsas("scan") != 0
    before = inst.snapshot()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage") == 1
    assert "Mirror of 'alpha' is damaged" in caplog.text
    assert "not on any disc yet" not in caplog.text
    assert inst.snapshot() == before

    # The by-hand repair: copy the pack back from the disc.
    (on_disc,) = [p for p in disc.rglob(tree.name) if p.is_file()]
    tree.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(on_disc, tree)
    inst.rustic("alpha", "check")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 0
    assert "damaged" not in caplog.text
    assert inst.lcsas("stage") == 0
    newest = inst.labels()[-1]
    for name in inst.repos:
        assert inst.disc_snapshots(newest, name) == inst.mirror_snapshots(name)


def test_consolidation_never_leaves_a_damaged_pack_behind(
    tmp_path: Path,
) -> None:
    """A damaged pack is not "pruned left behind": it stays in the plan,
    the consolidation stage refuses, and its volume cannot be
    deprecated while it is on no other volume."""
    from lcsas.config.media import MediaType
    from lcsas.consolidate.merger import VolumeMerger

    inst = _burned_install(tmp_path / "x")
    _tree, data = _pack_files(inst, "alpha")
    data.unlink()
    assert inst.lcsas("scan") != 0
    with inst.orchestrator() as orch:
        (vid,) = [int(r[0]) for r in orch._conn.execute(
            "SELECT volume_id FROM volumes")]
        plan = VolumeMerger(
            orch._conn, metadata_reserve_bytes=0,
            plan_volumes=orch.plan_volumes,
        ).plan_consolidation(
            [vid], MediaType.TEST_TINY)
        assert data.name not in {p.sha256 for p in plan.pruned_left_behind}
        assert data.name in {p.sha256 for p in plan.active_packs}
        with pytest.raises(StagingError, match="is damaged"):
            orch.stage(pack_sha256s=[p.sha256 for p in plan.active_packs],
                       for_location="Home_Shelf")
    label = inst.labels()[0]
    assert inst.lcsas("consolidate", label, "--media-type", "TEST_TINY",
                      "--deprecate") == 1
