"""#617: the windows-e2e / macos-e2e fixture's discs name sets as an owner's do.

Builds the real fixture (rustic + the burn pipeline + xorriso), small, in
windows-e2e's shape (two backup sets, the backup set metadata on the meta
disc too) and checks what is on the discs: every ``metadata/`` folder is
named by a UUID id, never by a name; its ``LCSAS_REPO.txt`` marker gives
the name and the name restore.bat shows (``name_cmd``); and ``sets.json``,
which the windows and macOS legs read, says the same.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from lcsas.staging.repo_marker import read_repo_marker

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "e2e"))
import fixture_lib  # noqa: E402

pytestmark = [
    pytest.mark.integration,
    pytest.mark.requires_rustic,
    pytest.mark.requires_xorriso,
]


def _metadata_of(iso: Path, dest: Path) -> dict[str, dict[str, str] | None]:
    """{folder name: its marker} for the iso's metadata/ folders."""
    subprocess.run(
        ["xorriso", "-osirrox", "on", "-indev", str(iso),
         "-extract", "/metadata", str(dest)],
        check=True, capture_output=True,
    )
    return {d.name: read_repo_marker(d) for d in dest.iterdir() if d.is_dir()}


def test_windows_e2e_fixture_discs_use_uuid_ids_and_names(tmp_path: Path) -> None:
    fx = fixture_lib.build_windows_fixture(
        tmp_path / "work",
        file_count=2,
        file_bytes=16 * 1024,
        meta_metadata=True,
        set_name=fixture_lib.WINDOWS_E2E_SET_NAME,
        other_set_names=fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES,
    )
    sets = json.loads(fx.sets_path.read_text(encoding="ascii"))
    every = [sets["restore"], *sets["others"]]
    assert [s["name"] for s in every] == [
        fixture_lib.WINDOWS_E2E_SET_NAME, *fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES,
    ]
    for s in every:
        assert str(uuid.UUID(s["id"])) == s["id"]
    restore = sets["restore"]
    assert restore["id"] == fx.restore_set.repo_id
    assert restore["name_cmd"] == "Fotos da fam_lia" != restore["name"]
    by_id = {s["id"]: s for s in every}

    # The meta disc carries every set; each data disc the sets it holds
    # packs of.  Every folder is an id, and its marker names the set.
    seen: set[str] = set()
    for n, iso in enumerate([fx.meta_iso, *fx.data_isos]):
        folders = _metadata_of(iso, tmp_path / f"x{n}")
        if iso == fx.meta_iso:
            assert set(folders) == set(by_id), iso.name
        for folder, marker in folders.items():
            assert folder in by_id, f"{iso.name}: metadata/{folder} is no set's id"
            assert marker is not None, f"{iso.name}: metadata/{folder} has no marker"
            want = by_id[folder]
            assert (marker["id"], marker["name"], marker["name_cmd"]) == (
                want["id"], want["name"], want["name_cmd"]
            ), iso.name
            seen.add(folder)
    assert restore["id"] in seen
    assert len(fx.data_isos) >= 1


def test_default_fixture_has_one_set_named_apart_from_its_id(
    tmp_path: Path,
) -> None:
    """macos-e2e's shape: the defaults, one set, no metadata on the meta
    disc (the data discs carry it)."""
    fx = fixture_lib.build_windows_fixture(
        tmp_path / "work", file_count=2, file_bytes=16 * 1024,
    )
    sets = json.loads(fx.sets_path.read_text(encoding="ascii"))
    assert sets["others"] == []
    assert sets["restore"]["name"] == fixture_lib.WINDOWS_SET_NAME
    assert sets["restore"]["id"] != sets["restore"]["name"]
    folders = _metadata_of(fx.data_isos[0], tmp_path / "x")
    assert list(folders) == [sets["restore"]["id"]]
    marker = folders[sets["restore"]["id"]]
    assert marker is not None and marker["name"] == fixture_lib.WINDOWS_SET_NAME
