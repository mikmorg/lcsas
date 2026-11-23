"""#965: stage a large first backup a few discs at a time.

``lcsas stage`` masters every volume of its plan before any is burned, so
a first large backup needed about 2.2 times its size in free staging
space.  ``stage --max-volumes N`` stages only the first N volumes of the
plan; the rest of the packs stay unarchived for the next run (stage N,
burn them, stage the next N).  A snapshot whose packs are not all staged
is left off these discs and carried by a later one.

The space preflight no longer counts the packs when the staging area and
every mirror share a file system (the staged tree is hardlinks), and
``stage --dry-run`` prints the space the real run needs and what is free.

Real rustic, real xorriso, every owner command through ``main()``, and
the heir's side through the real ``restore.sh`` of a META disc.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall, tree_digest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _five_volumes(root: Path) -> ScratchInstall:
    """alpha with five backups whose packs fill one TEST_TINY disc each."""
    inst = ScratchInstall(root, repos=("alpha",))
    for i in range(5):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def _unarchived(inst: ScratchInstall) -> int:
    (row,) = inst.query(
        "SELECT COUNT(*) FROM packs WHERE pack_id NOT IN "
        "(SELECT pack_id FROM volume_packs)")
    return int(row[0])


def test_batches_of_two_restore_the_newest_backup(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _five_volumes(tmp_path / "x")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 0
    assert "[DRY RUN] 5 volume(s) planned" in caplog.text

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--max-volumes", "2") == 0
    assert len(inst.labels()) == 2
    assert "Staging 2 of the 5 planned volumes (--max-volumes 2)" in caplog.text
    assert "3 more volume(s)" in caplog.text
    assert _unarchived(inst) > 0
    # The routine: stage, burn, stage.  (Whether a batch staged while this
    # one waits is refused depends on which snapshots it would complete,
    # so on how the packs were binned; the next test pins the rule with a
    # snapshot that spans every disc.)
    inst.mark_burned(inst.sessions()[-1])

    assert inst.lcsas("stage", "--max-volumes", "2") == 0
    assert len(inst.labels()) == 4
    assert _unarchived(inst) > 0
    inst.mark_burned(inst.sessions()[-1])

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--max-volumes", "2") == 0
    assert len(inst.labels()) == 5
    assert "planned volumes" not in caplog.text  # nothing left over
    assert _unarchived(inst) == 0
    inst.mark_burned(inst.sessions()[-1])

    # The newest disc carries the newest snapshot; all five discs
    # together restore it.
    newest = max(inst.labels())
    assert inst.disc_snapshots(newest, "alpha") == inst.mirror_snapshots("alpha")
    out = inst.root / "restored"
    run = inst.heir_restore("alpha", out)
    assert run.returncode == 0, run.stdout + run.stderr
    restored = tree_digest(out)
    expected = tree_digest(inst.data("alpha"))
    assert {k.rsplit("/", 1)[-1]: v for k, v in restored.items()
            if k.endswith(".bin")} == expected, (restored, expected)


@pytest.mark.parametrize("value", ["0", "-2"])
def test_max_volumes_below_one_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, value: str,
) -> None:
    inst = _five_volumes(tmp_path / "x")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--max-volumes", value) == 1
    assert "--max-volumes must be 1 or more" in caplog.text
    assert inst.labels() == []


def _needed(text: str) -> int:
    m = re.search(r"Free space the stage needs in the staging area: "
                  r"\S+ \S+ \(([\d,]+) bytes\)", text)
    assert m, text
    return int(m.group(1).replace(",", ""))


def test_dry_run_prints_space_and_counts_packs_only_when_copied(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lcsas.burn import orchestrator as orch_mod

    inst = _five_volumes(tmp_path / "x")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 0
    same = _needed(caplog.text)
    assert "free now:" in caplog.text
    assert str(inst.staging) in caplog.text

    # The staging area on another file system than the mirror: the packs
    # are copied, so they count.
    real = orch_mod._device_of

    def other_device(path: Path) -> int:
        dev = real(path)
        return dev + 1 if Path(path) == inst.staging else dev

    monkeypatch.setattr(orch_mod, "_device_of", other_device)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--dry-run") == 0
    copied = _needed(caplog.text)
    assert "copied, not hardlinked" in caplog.text
    (row,) = inst.query("SELECT SUM(size_bytes) FROM packs")
    assert copied - same == int(row[0])
    assert inst.labels() == []


def test_one_snapshot_across_batches_waits_for_the_last(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """A first backup that is one snapshot over five discs: no batch but
    the last carries it, and the batch that would complete it is refused
    while an earlier batch waits (#946), so the newest disc never lacks
    it."""
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    for i in range(5):
        inst.backup("alpha", f"big{i}.bin", size=700_000)
    inst.rustic("alpha", "forget", "--keep-last", "1")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    (snapshot,) = inst.mirror_snapshots("alpha")

    assert inst.lcsas("stage", "--max-volumes", "2") == 0
    first = inst.sessions()[-1]
    # Staged while the first waits: it does not complete the snapshot,
    # so nothing is left off for good yet.
    assert inst.lcsas("stage", "--max-volumes", "2") == 0
    second = inst.sessions()[-1]
    assert len(inst.labels()) == 4
    assert all(inst.disc_snapshots(label, "alpha") == set()
               for label in inst.labels())

    # The batch that would complete it is refused while they wait.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("stage", "--max-volumes", "2") == 1
    assert "waits to be burned" in caplog.text
    assert len(inst.labels()) == 4

    inst.mark_burned(first)
    inst.mark_burned(second)
    assert inst.lcsas("stage", "--max-volumes", "2") == 0
    newest = max(inst.labels())
    assert inst.disc_snapshots(newest, "alpha") == {snapshot}
