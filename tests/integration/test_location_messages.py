"""#968 items 1 to 6: what the location commands say, and what they leave.

1. ``init`` registers the config's default location, so plain ``burn``
   and ``stage --collection`` agree with plain ``stage``.
2. ``location status`` names repositories, not their ids.
3. ``location list`` and ``location status`` count the packs on no disc.
4. ``location rename`` prints the config line to change, and ``config
   check`` reports a default location that is not registered.
5. ``copy destroy`` of a disc that is not the volume's last prints the
   step that burns its packs again for that location.
6. Refusals name a volume by its label, not its catalog row id.

Real rustic, real xorriso, every command through ``main()``.
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


def _run(inst: ScratchInstall, caplog: pytest.LogCaptureFixture,
         *argv: str) -> tuple[int, str]:
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="lcsas"):
        rc = inst.lcsas(*argv)
    return rc, caplog.text


def _staged(root: Path, repos: tuple[str, ...] = ("alpha",),
            locations: tuple[str, ...] = ("Home_Shelf",)) -> ScratchInstall:
    inst = ScratchInstall(root, repos=repos, locations=locations)
    for name in repos:
        inst.backup(name, "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    return inst


def _set_default_location(inst: ScratchInstall, name: str) -> None:
    text = inst.config.read_text()
    lines = [f'location = "{name}"' if line.startswith("location = ") else line
             for line in text.splitlines()]
    inst.config.write_text("\n".join(lines) + "\n")


# -- 1 ---------------------------------------------------------------------

def test_init_registers_the_default_location(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    rc, out = _run(inst, caplog, "burn", "--session", session, "--dry-run")
    assert "Unknown location" not in out
    assert rc == 0, out
    assert [r[0] for r in inst.query("SELECT name FROM locations")] == [
        "Home_Shelf"]

    # The documented first step still works after init: not an error.
    rc, out = _run(inst, caplog, "location", "add", "Home_Shelf")
    assert rc == 0, out
    assert "Unexpected error" not in out
    assert "already registered" in out

    # A config that names a new default: init registers it too.
    _set_default_location(inst, "Offsite")
    assert inst.lcsas("init") == 0
    assert sorted(r[0] for r in inst.query("SELECT name FROM locations")) == [
        "Home_Shelf", "Offsite"]


def test_stage_collection_on_the_default_location_after_init(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = ScratchInstall(
        tmp_path / "x", repos=("alpha",),
        extra_config=('[collections.home]\nlocation = "Home_Shelf"\n'
                      'repos = ["alpha"]\n'),
    )
    inst.backup("alpha", "f1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    rc, out = _run(inst, caplog, "stage", "--collection", "home")
    assert "is not registered" not in out
    assert rc == 0, out
    assert len(inst.labels()) == 1


# -- 2 ---------------------------------------------------------------------

def test_location_status_names_repositories(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x", repos=("alpha", "beta"),
                   locations=("Home_Shelf", "Offsite"))
    (session,) = inst.sessions()
    inst.mark_burned(session)
    rc, out = _run(inst, caplog, "location", "status", "Offsite")
    assert rc == 0, out
    for name in ("alpha", "beta"):
        assert inst.repo_id(name) not in out
        assert f"{name}: " in out


# -- 3 ---------------------------------------------------------------------

def test_location_list_and_status_count_packs_on_no_disc(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)
    inst.backup("alpha", "f2.bin")
    assert inst.lcsas("scan") == 0
    ((n,),) = inst.query(
        "SELECT COUNT(*) FROM packs p WHERE p.is_pruned = 0 AND NOT EXISTS "
        "(SELECT 1 FROM volume_packs vp WHERE vp.pack_id = p.pack_id)")
    assert n > 0

    rc, out = _run(inst, caplog, "location", "list")
    assert rc == 0, out
    assert "all current" not in out
    assert f"{n} pack(s) are on no disc" in out

    rc, out = _run(inst, caplog, "location", "status", "Home_Shelf")
    assert rc == 0, out
    assert f"{n} pack(s) are on no disc" in out


# -- 4 ---------------------------------------------------------------------

def test_location_rename_points_at_the_config_and_config_check_sees_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x")
    (session,) = inst.sessions()
    inst.mark_burned(session)
    rc, out = _run(inst, caplog, "location", "rename", "Home_Shelf", "Shelf_B")
    assert rc == 0, out
    assert '[defaults] location = "Shelf_B"' in out

    rc, out = _run(inst, caplog, "config", "check")
    assert rc == 1, out
    assert "Configuration is valid" not in out
    assert "location 'Home_Shelf' is not a registered location" in out

    _set_default_location(inst, "Shelf_B")
    rc, out = _run(inst, caplog, "config", "check")
    assert rc == 0, out


def test_config_check_before_init_does_not_create_a_catalog(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = ScratchInstall(tmp_path / "x", repos=("alpha",))
    inst.db.unlink()
    rc, out = _run(inst, caplog, "config", "check")
    assert rc == 0, out
    assert not inst.db.exists()


# -- 5 ---------------------------------------------------------------------

def test_copy_destroy_of_one_disc_prints_the_restage_step(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x", locations=("Home_Shelf", "Offsite"))
    (session,) = inst.sessions()
    inst.mark_burned(session)
    inst.mark_burned(session, "Offsite")
    (label,) = inst.labels()
    for argv in (("copy", "destroy", label, "Offsite"),
                 ("copy", "deprecate", label, "Home_Shelf")):
        rc, out = _run(inst, caplog, *argv)
        assert rc == 0, out
        where = argv[3]
        assert f"lcsas stage --for-location {where}" in out, (argv, out)


# -- 6 ---------------------------------------------------------------------

def test_refusals_name_the_volume_by_label(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    inst = _staged(tmp_path / "x", locations=("Home_Shelf", "Offsite"))
    (session,) = inst.sessions()
    inst.mark_burned(session)
    inst.mark_burned(session, "Offsite")
    (label,) = inst.labels()
    ((vid,),) = inst.query("SELECT volume_id FROM volumes WHERE label = ?",
                           label)
    assert inst.lcsas("location", "add", "Bank") == 0
    for argv in (("location", "move", label, "--from", "Home_Shelf", "--to", "Offsite"),
                 ("copy", "deprecate", label, "Bank"),
                 ("copy", "destroy", label, "Bank"),
                 ("location", "move", label, "--from", "Bank", "--to", "Offsite")):
        rc, out = _run(inst, caplog, *argv)
        assert rc == 1, (argv, out)
        assert f"volume {vid}" not in out, (argv, out)
        assert label in out, (argv, out)
