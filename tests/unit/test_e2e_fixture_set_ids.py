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
from lcsas.db.volume_packs import link_pack_to_volume
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
            link_pack_to_volume(conn, vol.volume_id, pack.pack_id)
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
    the name fixture_lib gives it (the id is a fresh UUID each build)."""
    yml = (REPO_ROOT / ".github" / "workflows" / "macos-e2e.yml").read_text(
        encoding="utf-8"
    )
    want = f'--repo "{fixture_lib.WINDOWS_SET_NAME}"'
    assert yml.count(want) == 2
    assert "--repo win" not in yml
