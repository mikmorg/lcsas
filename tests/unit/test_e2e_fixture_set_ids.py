"""#617: the end-to-end fixtures register their backup sets as an owner does.

``lcsas repo add`` gives every set a UUID id, and the discs keep the set's
metadata in ``metadata/<id>/``.  Until rc6 the blind-restore fixture and
the windows-e2e / macos-e2e fixture registered ``repo_id == name``, so no
gate ever saw a menu entry, a lookup or a folder where the id and the name
differ, and drill #476 found the set menu showing UUIDs (#606).  These
pin the fixture builders: ids are UUIDs, names are the names, and every
lookup the blind fixture makes after registering goes by the name.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

from lcsas.db.connection import get_connection
from lcsas.db.packs import register_pack
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.volume_packs import bulk_link_packs
from lcsas.db.volumes import create_volume

REPO_ROOT = Path(__file__).resolve().parents[2]
E2E = REPO_ROOT / "tests" / "e2e"
HARNESS = E2E / "cdemu_blind_restore"

sys.path.insert(0, str(E2E))
import fixture_lib  # noqa: E402

_spec = importlib.util.spec_from_file_location("blind_setup", HARNESS / "setup.py")
assert _spec is not None and _spec.loader is not None
_setup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_setup)


def _is_uuid(text: str) -> bool:
    try:
        return str(uuid.UUID(text)) == text
    except ValueError:
        return False


def _rows(db: Path) -> list[tuple[str, str]]:
    conn = sqlite3.connect(db)
    try:
        return conn.execute(
            "SELECT repo_id, name FROM repositories ORDER BY name"
        ).fetchall()
    finally:
        conn.close()


def test_add_repo_gives_a_uuid_and_keeps_the_name(tmp_path: Path) -> None:
    db = tmp_path / "catalog.db"
    names = ("alpha", fixture_lib.WINDOWS_E2E_SET_NAME)
    got = {}
    for i, name in enumerate(names):
        mirror = tmp_path / f"m{i}"
        mirror.mkdir()
        got[name] = fixture_lib.add_repo(db, name, mirror)
    assert all(_is_uuid(i) for i in got.values())
    assert len(set(got.values())) == len(names)
    assert sorted(_rows(db), key=lambda r: r[1]) == sorted(
        ((i, n) for n, i in got.items()), key=lambda r: r[1]
    )


def test_add_repo_keeps_stdout_clean(tmp_path: Path, capsys) -> None:
    (tmp_path / "m").mkdir()
    fixture_lib.add_repo(tmp_path / "catalog.db", "alpha", tmp_path / "m")
    out, err = capsys.readouterr()
    assert out == ""
    assert "Registered repository 'alpha'" in err


def test_windows_e2e_set_names_make_the_menu_differ() -> None:
    """The restored set's name has a letter name_cmd turns into "_", so
    restore.bat's menu text (name_cmd) is not the name; the other set is
    there so the menu is shown at all."""
    from lcsas.staging.repo_marker import cmd_name

    name = fixture_lib.WINDOWS_E2E_SET_NAME
    assert cmd_name(name, "unused") != name
    assert cmd_name(name, "unused") == "Fotos da fam_lia"
    assert fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES
    assert name not in fixture_lib.WINDOWS_E2E_OTHER_SET_NAMES
    # The legs read the names from the fixture's sets.json; none is typed
    # into the script, which stays ASCII.
    ps1 = (E2E / "windows_e2e_lib.ps1").read_bytes()
    assert ps1.isascii()


def test_blind_fixture_registers_uuid_ids_named_after_the_tenants(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("LCSAS_TENANT_COUNT", "3")
    mirror = tmp_path / "mirror"
    for name in ("alpha", "bravo", "charlie"):
        (mirror / name / "data").mkdir(parents=True)
    monkeypatch.setattr(_setup, "MIRROR", mirror)
    monkeypatch.setattr(_setup, "DB_PATH", tmp_path / "catalog.db")

    conn = _setup._init_burn_db()
    try:
        rows = _rows(tmp_path / "catalog.db")
        assert [n for _, n in rows] == ["alpha", "bravo", "charlie"]
        for repo_id, name in rows:
            assert _is_uuid(repo_id), repo_id
            assert _setup._repo_id(conn, name) == repo_id
        with pytest.raises(LookupError):
            _setup._repo_id(conn, "delta")
    finally:
        conn.close()


def test_expected_alpha_volumes_are_found_by_name(
    tmp_path: Path, monkeypatch,
) -> None:
    """verify.sh's expected_alpha_volumes.txt: alpha's discs, looked up by
    its NAME (its id is a UUID).  An empty file would make verify.sh
    refuse to score every run."""
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    create_all(conn)
    ids = {
        "alpha": "0b6e3c2a-91f4-4d7e-8a35-6c2f1e9d4b70",
        "bravo": "e4a19d07-3b6c-4f28-9e51-d2c80a7b6f13",
    }
    for name, repo_id in ids.items():
        register_repo(conn, repo_id, name, f"/m/{name}")
    layout = {
        "LCSAS_TEST_TINY_2026_0001": ("alpha", "bravo"),
        "LCSAS_TEST_TINY_2026_0002": ("bravo",),
        "LCSAS_TEST_TINY_2026_0003": ("alpha",),
    }
    n = 0
    for label, owners in layout.items():
        vol = create_volume(conn, label, f"uuid-{label}", "TEST_TINY", 2 << 20)
        for owner in owners:
            n += 1
            pack = register_pack(conn, f"{n:064x}", 1000, ids[owner])
            bulk_link_packs(conn, vol.volume_id, [pack.pack_id])
    conn.close()
    monkeypatch.setattr(_setup, "DB_PATH", db)
    monkeypatch.setattr(_setup, "FIXTURE", tmp_path)

    labels = _setup._write_expected_alpha_volumes()

    want = ["LCSAS_TEST_TINY_2026_0001", "LCSAS_TEST_TINY_2026_0003"]
    assert labels == want
    assert (tmp_path / "expected_alpha_volumes.txt").read_text() == (
        "\n".join(want) + "\n"
    )


def test_expected_alpha_volumes_refuses_an_empty_list(
    tmp_path: Path, monkeypatch,
) -> None:
    db = tmp_path / "catalog.db"
    conn = get_connection(db)
    create_all(conn)
    register_repo(conn, "3d9f7a21-6b0e-4c58-b2a4-81e5f0c3d69a", "alpha", "/m/a")
    conn.close()
    monkeypatch.setattr(_setup, "DB_PATH", db)
    monkeypatch.setattr(_setup, "FIXTURE", tmp_path)
    with pytest.raises(SystemExit):
        _setup._write_expected_alpha_volumes()
    assert not (tmp_path / "expected_alpha_volumes.txt").exists()


@pytest.mark.parametrize("path", [
    HARNESS / "setup.py",
    E2E / "fixture_lib.py",
])
def test_fixture_builders_register_sets_only_with_repo_add(path: Path) -> None:
    """A direct register_repo() call can give a set its name as its id
    again; add_repo() runs `lcsas repo add`."""
    assert "register_repo(" not in path.read_text(encoding="utf-8")


def test_macos_e2e_names_the_fixture_set_by_its_name() -> None:
    """macos-e2e.yml's LCSAS_REPO legs pass the set's name, which must be
    the name fixture_lib gives it (the id is a fresh UUID each build).

    The macOS fixture has ONE set, and restore.sh takes a lone set without
    reading LCSAS_REPO, so these legs do not test name matching (that is
    test_restore_sh_journey's job).  No step may claim they do."""
    yml = (REPO_ROOT / ".github" / "workflows" / "macos-e2e.yml").read_text(
        encoding="utf-8"
    )
    want = f'--repo "{fixture_lib.WINDOWS_SET_NAME}"'
    assert yml.count(want) == 2
    assert "--repo win" not in yml
    assert "LCSAS_REPO by name" not in yml
    assert "other_set_names" not in (E2E / "macos_restore_smoke.py").read_text(
        encoding="utf-8"
    )


def _pr_paths(workflow: str) -> list[str]:
    """The `on: pull_request: paths:` entries of a workflow (no YAML
    library: the project's tests run on the standard library)."""
    text = (REPO_ROOT / ".github" / "workflows" / workflow).read_text(
        encoding="utf-8"
    )
    block = text.split("  pull_request:\n    paths:\n", 1)[1]
    paths = []
    for line in block.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if not stripped.startswith("- "):
            break
        paths.append(stripped[2:].strip().strip('"'))
    return paths


@pytest.mark.parametrize("workflow", ["windows-e2e.yml", "macos-e2e.yml"])
def test_e2e_workflows_run_when_the_set_marker_changes(workflow: str) -> None:
    """Both fixtures put LCSAS_REPO.txt on every disc, and the Windows legs
    expect its name_cmd (repo_marker.cmd_name): a PR that changes either
    must run the journeys, not wait for the weekly run."""
    paths = _pr_paths(workflow)
    assert "tests/e2e/fixture_lib.py" in paths  # the parser found the list
    assert "src/lcsas/staging/repo_marker.py" in paths


# ---------------------------------------------------------------------------
# The discs' set folders (fixture_lib.set_marker_problems; the blind
# setup runs it on every disc it stages)
# ---------------------------------------------------------------------------

ALPHA_ID = "6f1c2b9e-3d47-4a85-b0e2-9c7d15a3f468"
BRAVO_ID = "a2d84e17-5c90-4b3f-8e61-07b9f4c2d5ae"


def _set_folder(metadata: Path, repo_id: str, name: str | None) -> Path:
    from lcsas.staging.repo_marker import make_repo_marker, write_repo_marker

    folder = metadata / repo_id
    (folder / "keys").mkdir(parents=True)
    if name is not None:
        write_repo_marker(folder, make_repo_marker(repo_id, catalog_name=name))
    return folder


def test_set_marker_problems_accepts_an_owners_folders(tmp_path: Path) -> None:
    md = tmp_path / "metadata"
    _set_folder(md, ALPHA_ID, "alpha")
    _set_folder(md, BRAVO_ID, "bravo")
    names = ("alpha", "bravo")
    assert fixture_lib.set_marker_problems(md, names, require_all=True) == []
    # A data disc may hold only some of the sets.
    (md / BRAVO_ID / "LCSAS_REPO.txt").unlink()
    (md / BRAVO_ID / "keys").rmdir()
    (md / BRAVO_ID).rmdir()
    assert fixture_lib.set_marker_problems(md, names, require_all=False) == []
    assert fixture_lib.set_marker_problems(md, names, require_all=True) == [
        f"{md}: no folder for bravo"
    ]


def _named_by_name(md: Path) -> None:
    (md / "alpha" / "keys").mkdir(parents=True)


def _no_marker(md: Path) -> None:
    _set_folder(md, ALPHA_ID, None)


def _marker_of_another_folder(md: Path) -> None:
    folder = _set_folder(md, ALPHA_ID, None)
    other = _set_folder(md.parent / "elsewhere", BRAVO_ID, "alpha")
    (folder / "LCSAS_REPO.txt").write_bytes((other / "LCSAS_REPO.txt").read_bytes())


def _foreign_name(md: Path) -> None:
    _set_folder(md, ALPHA_ID, "zulu")


def _same_name_twice(md: Path) -> None:
    _set_folder(md, ALPHA_ID, "alpha")
    _set_folder(md, BRAVO_ID, "alpha")


def _no_folders(md: Path) -> None:
    md.mkdir(parents=True)


@pytest.mark.parametrize(("make", "says"), [
    (_named_by_name, "not named by a UUID id (it is the set's name)"),
    (_no_marker, "no LCSAS_REPO.txt whose id= is the folder"),
    (_marker_of_another_folder, "no LCSAS_REPO.txt whose id= is the folder"),
    (_foreign_name, "the marker names 'zulu', no set of this archive"),
    (_same_name_twice, "'alpha' also has the folder"),
    (_no_folders, "no backup set folders"),
])
def test_set_marker_problems_names_what_is_wrong(
    tmp_path: Path, make, says: str,
) -> None:
    md = tmp_path / "metadata"
    make(md)
    problems = fixture_lib.set_marker_problems(
        md, ("alpha", "bravo"), require_all=False,
    )
    assert any(says in p for p in problems), problems


def test_blind_setup_stops_on_a_disc_named_by_set_names(
    tmp_path: Path, monkeypatch,
) -> None:
    """The blind fixture's discs are checked as they are staged: the
    pre-rc6 layout (metadata/alpha/) stops the setup."""
    monkeypatch.setenv("LCSAS_TENANT_COUNT", "2")
    good = tmp_path / "good"
    _set_folder(good / "metadata", ALPHA_ID, "alpha")
    _setup._check_set_markers(good, require_all=False)
    with pytest.raises(SystemExit, match="no folder for bravo"):
        _setup._check_set_markers(good, require_all=True)
    old = tmp_path / "old"
    _named_by_name(old / "metadata")
    with pytest.raises(SystemExit, match="not named by a UUID id"):
        _setup._check_set_markers(old, require_all=False)


def test_blind_setup_checks_every_disc_before_mastering_it() -> None:
    """Each data disc's staging tree, and the META disc's, is checked
    after the variant edits and before its ISO is mastered."""
    import inspect

    # stage() injects the catalog and masters the ISO in one call, so the
    # data-disc hook wraps xorriso's create_iso (#699).
    burn = inspect.getsource(_setup._run_burn_pipeline)
    check = burn.index("_check_set_markers(Path(source_dir), require_all=False)")
    assert burn.index("_apply_data_disc_variant_mutations(") < check
    assert check < burn.index("return self._inner.create_iso(")
    assert "VariantXorriso(SubprocessXorrisoRunner())" in burn
    meta = inspect.getsource(_setup._build_meta_iso)
    check = meta.index("_check_set_markers(META_STAGE, require_all=True)")
    assert meta.index("_apply_variant_mutations(META_STAGE)") < check
    assert check < meta.index("master_iso(")


# ---------------------------------------------------------------------------
# windows-e2e's multi-disc proof (fixture_lib.restore_set_span_problem)
# ---------------------------------------------------------------------------


def _sets_json(tmp_path: Path, discs: dict[str, list[str]]) -> Path:
    import json

    p = tmp_path / "sets.json"
    p.write_text(json.dumps({"restore": {}, "others": [], "restore_discs": discs}))
    return p


@pytest.mark.parametrize(("discs", "says"), [
    ({"D1": ["a", "b"], "D2": ["c"]}, None),
    ({"D1": ["a", "b"], "D2": ["b"]}, "D1 alone holds all 2 packs"),
    ({"D1": ["a", "b", "c"]}, "lie on 1 data disc(s) (D1); need >= 2"),
    ({}, "no disc holds a pack of the restored set"),
])
def test_restore_set_span_problem(
    tmp_path: Path, discs: dict[str, list[str]], says: str | None,
) -> None:
    got = fixture_lib.restore_set_span_problem(_sets_json(tmp_path, discs))
    if says is None:
        assert got is None
    else:
        assert got is not None and says in got, got


def test_windows_e2e_fixture_job_checks_the_restored_set_spans_discs() -> None:
    """The fixture is built at fixture_lib's windows-e2e size (which the
    integration test builds too), and the job fails unless the restored
    set's own packs need two discs or more."""
    yml = (REPO_ROOT / ".github" / "workflows" / "windows-e2e.yml").read_text(
        encoding="utf-8"
    )
    assert "file_count=fixture_lib.WINDOWS_E2E_FILE_COUNT" in yml
    assert "file_bytes=fixture_lib.WINDOWS_E2E_FILE_BYTES" in yml
    assert "restore_set_span_problem(fx.sets_path, minimum=2)" in yml
