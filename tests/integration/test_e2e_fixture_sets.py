"""#617: the windows-e2e / macos-e2e fixture's discs name sets as an owner's do.

Builds the real fixture (rustic + the burn pipeline + xorriso) exactly as
windows-e2e's fixture job does (two backup sets, the backup set metadata
on the meta disc too, the workflow's size) and checks what is on the
discs: every ``metadata/`` folder is named by a UUID id, never by a name;
its ``LCSAS_REPO.txt`` marker gives the name and the name restore.bat
shows (``name_cmd``); ``sets.json``, which the windows and macOS legs
read, says the same; and the restored set's own packs lie on two discs or
more, which windows-e2e's multi-disc step relies on.

The second set's password is split 2-of-3 as an owner splits it (#607):
``lcsas key split`` records the split, so every disc's marker for that
set says ``split=2-of-3`` with the cards' split id, the first set's says
``split=none``, and two of the printed cards rebuild the password.
windows-e2e types two of those cards into restore.bat on real Windows.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from lcsas.keyshare import decode_master_secret, extract_mnemonic, recover_secret
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
    # The extracted folders keep the disc's read-only modes; make them
    # writable again, or pytest cannot remove its temp folder.
    for dirpath, _dirs, _files in os.walk(dest):
        os.chmod(dirpath, 0o755)
    return {d.name: read_repo_marker(d) for d in dest.iterdir() if d.is_dir()}


def _packs_of(iso: Path) -> set[str]:
    """The pack file names under the iso's /data."""
    proc = subprocess.run(
        ["xorriso", "-indev", str(iso), "-find", "/data", "-type", "f"],
        check=True, capture_output=True, text=True,
    )
    return {
        line.strip().strip("'").rsplit("/", 1)[-1]
        for line in proc.stdout.splitlines()
        if line.startswith("'/data/")
    }


def test_windows_e2e_fixture_discs_use_uuid_ids_and_names(tmp_path: Path) -> None:
    """windows-e2e's fixture exactly as the workflow builds it."""
    fx = fixture_lib.build_windows_fixture(
        tmp_path / "work",
        file_count=fixture_lib.WINDOWS_E2E_FILE_COUNT,
        file_bytes=fixture_lib.WINDOWS_E2E_FILE_BYTES,
        meta_metadata=True,
        set_name=fixture_lib.WINDOWS_E2E_SET_NAME,
        other_set_names=fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES,
        split_sets=fixture_lib.WINDOWS_E2E_SPLIT_SETS,
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
    names = [s["name"] for s in every]
    for n, iso in enumerate([fx.meta_iso, *fx.data_isos]):
        folders = _metadata_of(iso, tmp_path / f"x{n}")
        if iso == fx.meta_iso:
            assert set(folders) == set(by_id), iso.name
        # The blind setup's check of each disc agrees.
        assert fixture_lib.set_marker_problems(
            tmp_path / f"x{n}", names, require_all=iso == fx.meta_iso,
        ) == [], iso.name
        for folder, marker in folders.items():
            assert folder in by_id, f"{iso.name}: metadata/{folder} is no set's id"
            assert marker is not None, f"{iso.name}: metadata/{folder} has no marker"
            want = by_id[folder]
            assert (marker["id"], marker["name"], marker["name_cmd"]) == (
                want["id"], want["name"], want["name_cmd"]
            ), iso.name
            # #607: the split as recorded, on every disc that has the set.
            assert (marker["split"], marker.get("split_id", "")) == (
                want["split"], want.get("split_id", "")
            ), (iso.name, want["name"])
            seen.add(folder)
    assert restore["id"] in seen

    # The multi-disc proof (#517, #617): the restored set's OWN packs need
    # two discs or more, as the workflow's fixture job asserts; and
    # restore_discs, which says so, matches the packs on the discs.
    assert len(fx.data_isos) >= 2
    assert fixture_lib.restore_set_span_problem(fx.sets_path, minimum=2) is None
    discs = sets["restore_discs"]
    restored = {sha for packs in discs.values() for sha in packs}
    on_discs = {iso.stem: _packs_of(iso) for iso in fx.data_isos}
    assert set(discs) <= set(on_discs)
    for label, packs in on_discs.items():
        assert sorted(packs & restored) == discs.get(label, []), label
    # The other set's packs share the discs: a count of every pack (what
    # Assert-MultiDisc sees on Windows) is more than the restored set's.
    assert set().union(*on_discs.values()) > restored

    # #607: the restored set is not split; the other one is, 2-of-3, with
    # its three printed cards and a manifest for windows-e2e's cards leg.
    assert restore["split"] == "none" and "cards" not in restore
    work = next(s for s in every if s["name"] in fixture_lib.WINDOWS_E2E_SPLIT_SETS)
    k, n = fixture_lib.WINDOWS_E2E_SPLIT_SETS[work["name"]]
    assert (k, n) == (2, 3)
    assert work["split"] == "2-of-3"
    assert len(work["split_id"]) == 5 and work["split_id"].isdigit()
    assert [s["name"] for s in every if s["split"] != "none"] == [work["name"]]
    workdir = tmp_path / "work"
    cards = [workdir / c for c in work["cards"]]
    assert len(cards) == n and all(c.is_file() for c in cards), cards
    texts = [c.read_text(encoding="utf-8") for c in cards]
    for i, text in enumerate(texts, 1):
        assert f"Repository : {work['name']}" in text
        assert f"Share      : {i} of {n}" in text
        assert f"Split ID   : {work['split_id']}" in text
    # Card 1 and card 3, the two the leg types, rebuild the password.
    password = (workdir / "other1.pw").read_bytes()
    rebuilt = decode_master_secret(recover_secret(
        [extract_mnemonic(texts[0]), extract_mnemonic(texts[2])]
    ))
    assert rebuilt == password
    manifest = json.loads((workdir / work["manifest"]).read_text(encoding="utf-8"))
    assert sorted(manifest) == ["file_000.bin", "file_001.bin"]
    assert restore["manifest"] == "manifest.json"
    assert json.loads(fx.manifest_path.read_text(encoding="utf-8")) == (
        fixture_lib.sha256_tree_manifest(fx.source_root)
    )


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
    assert sets["restore"]["split"] == "none"
    assert not (tmp_path / "work" / "cards").exists()
    assert sets["restore"]["name"] == fixture_lib.WINDOWS_SET_NAME
    assert sets["restore"]["id"] != sets["restore"]["name"]
    folders = _metadata_of(fx.data_isos[0], tmp_path / "x")
    assert list(folders) == [sets["restore"]["id"]]
    marker = folders[sets["restore"]["id"]]
    assert marker is not None and marker["name"] == fixture_lib.WINDOWS_SET_NAME
