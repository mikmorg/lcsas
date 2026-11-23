"""#968 items 20 to 23: scan, packs and repositories.

20. ``scan`` exited 0 when the snapshot listing failed for a repository.
21. ``pack unprune`` accepted a pack that is not in the mirror, with no
    word; the next scan marked it pruned again.
22. ``repo add x /nonexistent`` registered it.  A path with no rustic
    repository (no ``config`` file) is refused unless ``--force``.
23. Renaming a ``[repos.X]`` table in the config only: new discs named
    the set by the new name while the catalog and the old discs kept the
    old one, and ``config check`` said nothing.  It now warns.

Real rustic (a real prune), every command through ``main()``.
"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from tests.integration.scratch_install import ScratchInstall

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _install(root: Path) -> ScratchInstall:
    inst = ScratchInstall(root, repos=("alpha",))
    inst.backup("alpha", "f1.bin", size=60_000)
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    return inst


def _pack_names(inst: ScratchInstall) -> set[str]:
    return {p.name for p in (inst.mirror("alpha") / "data").rglob("*")
            if p.is_file()}


def _is_pruned(inst: ScratchInstall, sha: str) -> int:
    (row,) = inst.query("SELECT is_pruned FROM packs WHERE sha256 = ?", sha)
    return int(row[0])


# -- 20 ---------------------------------------------------------------------

def test_scan_exits_non_zero_when_a_snapshot_listing_fails(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x")
    inst.key("alpha").write_text("not the password\n")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("scan") == 1
    assert "Snapshot listing failed for: alpha" in caplog.text
    # Pack registration, the rest of the scan, still happened.
    assert "Total scanned: " in caplog.text


# -- 21 ---------------------------------------------------------------------

def test_unprune_refuses_a_pack_rustic_pruned_from_the_mirror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x")
    (first,) = inst.mirror_snapshots("alpha")
    before = _pack_names(inst)
    (inst.data("alpha") / "f1.bin").unlink()
    inst.backup("alpha", "f2.bin", size=60_000)
    inst.rustic("alpha", "forget", first)
    inst.rustic("alpha", "prune", "--max-unused", "0", "--instant-delete")
    gone = before - _pack_names(inst)
    assert gone
    assert inst.lcsas("scan") == 0
    sha = sorted(gone)[0]
    assert _is_pruned(inst, sha) == 1

    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("pack", "unprune", sha[:16]) == 1
    assert f"Pack {sha} is not in the mirror of 'alpha'" in caplog.text
    assert "the next 'lcsas scan' would mark it pruned again" in caplog.text
    assert _is_pruned(inst, sha) == 1


def test_unprune_still_restores_a_pack_that_is_in_the_mirror(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    """The command's job: a pack marked pruned by mistake (BURN-09) whose
    file is in the mirror goes back to the active pool."""
    import sqlite3

    inst = _install(tmp_path / "x")
    sha = sorted(_pack_names(inst))[0]
    conn = sqlite3.connect(inst.db)
    conn.execute("UPDATE packs SET is_pruned = 1 WHERE sha256 = ?", (sha,))
    conn.commit()
    conn.close()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("pack", "unprune", sha) == 0
    assert "restored to the active pool" in caplog.text
    assert _is_pruned(inst, sha) == 0


# -- 22 ---------------------------------------------------------------------

@pytest.mark.parametrize("where", ["missing", "empty-folder"])
def test_repo_add_refuses_a_path_with_no_repository(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, where: str,
) -> None:
    inst = _install(tmp_path / "x")
    path = tmp_path / where
    if where == "empty-folder":
        path.mkdir()
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("repo", "add", "other", str(path)) == 1
    assert f"{path} holds no rustic repository (no 'config' file)" in caplog.text
    assert "--force" in caplog.text
    assert not inst.query("SELECT 1 FROM repositories WHERE name = 'other'")


def test_repo_add_force_registers_a_mirror_not_there_yet(
    tmp_path: Path,
) -> None:
    inst = _install(tmp_path / "x")
    path = tmp_path / "not-mounted-yet"
    assert inst.lcsas("repo", "add", "other", str(path), "--force") == 0
    assert inst.query("SELECT 1 FROM repositories WHERE name = 'other'")


# -- 23 ---------------------------------------------------------------------

def test_config_check_reports_a_table_renamed_in_the_config_only(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _install(tmp_path / "x")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert inst.lcsas("config", "check") == 0
    assert "Configuration is valid." in caplog.text

    renamed = tmp_path / "x" / "renamed.toml"
    renamed.write_text(inst.config.read_text().replace(
        "[repos.alpha]", "[repos.photos]"))
    from lcsas.cli.main import main
    caplog.clear()
    with caplog.at_level(logging.INFO):
        main(["--config", str(renamed), "config", "check"])
    # A warning: a key that differs from the catalog name is resolved by
    # its mirror path on purpose (#687).
    warned = [r.getMessage() for r in caplog.records
              if r.levelno == logging.WARNING]
    assert any(
        f"[repos.photos]: its mirror {inst.mirror('alpha')} is registered "
        "in the catalog as 'alpha'" in w for w in warned), caplog.text
