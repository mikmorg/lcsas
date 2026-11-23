"""#962: no command makes the catalog forget packs that are on burned discs.

A repository's id is minted by ``lcsas repo add`` and burned onto every
disc (``metadata/<repo_id>/``, ``packs.repo_id`` in the disc catalog).
``repo remove --force`` deleted the rows of a repository whose packs were
on burned volumes; ``repo add`` of the same mirror then minted a new id,
and ``stage`` queued every pack to burn again under a second identity.

Now ``repo remove`` (with or without ``--force``) refuses a repository with
a pack on a burned volume and points at ``repo retire``; ``repo add`` of a
retired repository's mirror says so and offers ``repo activate``; and a
permitted remove recomputes ``volumes.used_bytes`` and leaves no orphan row.

Real rustic, real xorriso, every command through ``main()``.
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]

_TABLES = ("repositories", "packs", "volume_packs", "snapshots", "volumes",
           "key_escrow")


def _install(root: Path) -> ScratchInstall:
    """alpha and bravo backed up, registered, scanned and staged."""
    inst = ScratchInstall(root, repos=("alpha", "bravo"))
    for name in inst.repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    return inst


def _catalog(inst: ScratchInstall) -> dict[str, list[tuple[object, ...]]]:
    """Every row of the tables a repository's removal touches."""
    return {t: sorted(tuple(r) for r in inst.query(f"SELECT * FROM {t}"))
            for t in _TABLES}


def _confirm_yes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")


def _unqueued(inst: ScratchInstall) -> int:
    """Packs a ``stage`` would queue: not pruned and on no volume."""
    (row,) = inst.query(
        "SELECT COUNT(*) FROM packs p WHERE p.is_pruned = 0 AND NOT EXISTS "
        "(SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id)")
    return int(row[0])


def test_remove_of_a_repository_with_burned_packs_is_refused(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inst = _install(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)
    _confirm_yes(monkeypatch)
    before = _catalog(inst)
    assert any(r for r in before["packs"])

    for argv in (("repo", "remove", "bravo", "--force"),
                 ("repo", "remove", "bravo")):
        caplog.clear()
        with caplog.at_level(logging.INFO):
            assert inst.lcsas(*argv) == 1, argv
        assert "1 burned volume" in caplog.text, caplog.text
        assert "lcsas repo retire bravo" in caplog.text
        assert "Nothing was deleted" in caplog.text
        assert _catalog(inst) == before

    # The way the refusal points at keeps the identity and the packs.
    bravo = inst.repo_id("bravo")
    assert inst.lcsas("repo", "retire", "bravo") == 0
    assert inst.repo_id("bravo") == bravo
    assert _catalog(inst)["packs"] == before["packs"]


def test_repo_add_of_a_retired_repository_offers_activate(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)
    bravo = inst.repo_id("bravo")
    assert inst.lcsas("repo", "retire", "bravo") == 0
    repos_before = _catalog(inst)["repositories"]

    # The same mirror again, under the same name ...
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("repo", "add", "bravo", str(inst.mirror("bravo"))) == 1
    assert "retired" in caplog.text, caplog.text
    assert "lcsas repo activate bravo" in caplog.text
    assert bravo in caplog.text

    # ... and a copy of it under another name and path: its keys match.
    moved = inst.root / "repos" / "bravo-moved"
    shutil.copytree(inst.mirror("bravo"), moved)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("repo", "add", "bravo2", str(moved)) == 1
    assert "retired repository 'bravo'" in caplog.text, caplog.text
    assert "lcsas repo activate bravo" in caplog.text
    assert _catalog(inst)["repositories"] == repos_before

    # activate restores it with the same id, and nothing is queued again.
    assert inst.lcsas("repo", "activate", "bravo") == 0
    assert inst.repo_id("bravo") == bravo
    assert inst.lcsas("scan") == 0
    assert _unqueued(inst) == 0


def test_remove_of_a_never_burned_repository_leaves_no_orphans(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inst = _install(tmp_path / "x")  # staged, not burned
    bravo = inst.repo_id("bravo")
    alpha = inst.repo_id("alpha")
    _confirm_yes(monkeypatch)

    with caplog.at_level(logging.INFO):
        assert inst.lcsas("repo", "remove", "bravo", "--force") == 0
    assert "pruned" not in caplog.text, caplog.text
    # The staged ISO image still holds bravo's packs: the owner is told.
    (label,) = inst.labels()
    assert f"staged volume(s) {label} still hold its packs" in caplog.text
    assert "lcsas session abort" in caplog.text
    assert "Deleted repository 'bravo' from the catalog, with 2 pack(s)" in caplog.text

    for table in ("repositories", "packs", "snapshots", "key_escrow"):
        assert not inst.query(
            f"SELECT 1 FROM {table} WHERE repo_id = ?", bravo), table
    assert not inst.query(
        "SELECT 1 FROM volume_packs vp WHERE NOT EXISTS "
        "(SELECT 1 FROM packs p WHERE p.pack_id = vp.pack_id)")
    assert not inst.query("PRAGMA foreign_key_check")

    # Every volume's used_bytes is the size of the packs it still holds.
    vols = inst.query("SELECT volume_id, used_bytes FROM volumes")
    assert vols
    for vol in vols:
        (row,) = inst.query(
            "SELECT COALESCE(SUM(p.size_bytes), 0) FROM volume_packs vp "
            "JOIN packs p ON p.pack_id = vp.pack_id WHERE vp.volume_id = ?",
            vol["volume_id"])
        assert vol["used_bytes"] == row[0]
    assert inst.query("SELECT 1 FROM packs WHERE repo_id = ?", alpha)
