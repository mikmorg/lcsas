"""Repository resolution, duplicate repositories and split records.

- #685: ``repo add`` refuses a duplicate name or mirror path, and
  ``_resolve_repo_names_to_ids`` refuses an ambiguous name.
- #687: one resolver (mirror path, then name) for every ``--repo``; a
  repository renamed in the config resolves the same in ``key split`` and
  ``stage``.
- #683: ``key split`` refuses a folder that already holds share files for
  the repository.
- #681: ``key unsplit`` clears a recorded split; the drift error names it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lcsas.cli.main import _resolve_repo_names_to_ids, main
from lcsas.config.settings import load_config
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.key_escrow import (
    config_names_by_repo_id,
    detect_escrow_drift,
    get_split,
    get_split_for_repo,
    record_split,
)
from lcsas.db.packs import register_pack
from lcsas.db.repos import list_repos, register_repo, resolve_repo
from lcsas.db.schema import create_all
from tests.unit.test_cli_key import PASSWORD, _make_key_file, _write_pw_file

# --------------------------------------------------------------------------
# #685: duplicates
# --------------------------------------------------------------------------


def _repo_add(db: Path, name: str, mirror: Path) -> int:
    mirror.mkdir(parents=True, exist_ok=True)
    return main(["--db", str(db), "repo", "add", name, str(mirror)])


def test_repo_add_refuses_an_existing_name(tmp_path, capsys):
    db = tmp_path / "archive.db"
    assert _repo_add(db, "photos", tmp_path / "m1") == 0
    capsys.readouterr()

    assert _repo_add(db, "photos", tmp_path / "m2") == 1

    out = capsys.readouterr().out
    assert "name 'photos' is already registered" in out
    assert str(tmp_path / "m1") in out  # names the existing repository
    conn = get_connection(db)
    assert len(list_repos(conn)) == 1
    conn.close()


def test_repo_add_refuses_an_existing_mirror_path(tmp_path, capsys):
    db = tmp_path / "archive.db"
    assert _repo_add(db, "photos", tmp_path / "m1") == 0
    capsys.readouterr()

    assert _repo_add(db, "pictures", tmp_path / "m1") == 1

    out = capsys.readouterr().out
    assert "mirror path" in out and "'photos'" in out
    conn = get_connection(db)
    assert [r.name for r in list_repos(conn)] == ["photos"]
    conn.close()


def test_resolve_names_raises_on_an_ambiguous_name():
    conn = get_memory_connection()
    create_all(conn)
    # Two rows with one name: only possible in a catalog made before #685.
    register_repo(conn, "id-1", "photos", "/m/1")
    register_repo(conn, "id-2", "photos", "/m/2")

    with pytest.raises(ValueError, match="More than one repository"):
        _resolve_repo_names_to_ids(conn, ["photos"])
    conn.close()


# --------------------------------------------------------------------------
# #687: one resolver
# --------------------------------------------------------------------------


def test_resolver_order_mirror_path_then_name_then_id():
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "id-a", "photos", "/m/photos")
    register_repo(conn, "id-b", "pics", "/m/other")

    # The config key 'pics' points at photos' mirror: the mirror wins.
    assert resolve_repo(conn, "pics", Path("/m/photos/")).repo_id == "id-a"
    # Without a config, the name.
    assert resolve_repo(conn, "pics").repo_id == "id-b"
    # The catalog ID, then the name ignoring case.
    assert resolve_repo(conn, "id-a").repo_id == "id-a"
    assert resolve_repo(conn, "PHOTOS").repo_id == "id-a"
    assert resolve_repo(conn, "nothing") is None
    # The burn's reverse map puts the mirror first too.
    names = config_names_by_repo_id(conn, {"pics": Path("/m/photos")})
    assert names["id-a"] == "pics"
    conn.close()


def _renamed_setup(tmp_path: Path) -> tuple[Path, Path, str]:
    """Catalog repository 'photos'; the config calls the same mirror 'pics'."""
    mirror = tmp_path / "mirror" / "photos"
    for sub in ("keys", "data", "index", "snapshots"):
        (mirror / sub).mkdir(parents=True, exist_ok=True)
    _make_key_file(mirror / "keys", PASSWORD)
    pw = _write_pw_file(tmp_path / "photos.pw")
    (tmp_path / "staging").mkdir()
    db = tmp_path / "archive.db"
    cfg = tmp_path / "lcsas.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\ndatabase = "{db}"\n'
        '[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
        f'[repos.pics]\nmirror_path = "{mirror}"\npassword_file = "{pw}"\n'
    )
    assert main(["--db", str(db), "repo", "add", "photos", str(mirror)]) == 0
    conn = get_connection(db)
    repo_id = list_repos(conn)[0].repo_id
    register_pack(conn, "a" * 64, 1000, repo_id)
    conn.close()
    return cfg, db, repo_id


def test_renamed_repository_resolves_the_same_in_key_split_and_stage(
    tmp_path, capsys, monkeypatch,
):
    cfg, db, repo_id = _renamed_setup(tmp_path)
    monkeypatch.chdir(tmp_path)

    # key split --repo pics records the split under photos' repo_id.
    assert main(["--config", str(cfg), "key", "split", "--repo", "pics",
                 "-k", "2", "-n", "3", "--out", str(tmp_path / "cards")]) == 0
    conn = get_connection(db)
    record = get_split(conn, repo_id)
    assert record is not None and (record.threshold, record.shares) == (2, 3)

    # stage --repo pics selects the same repository.
    assert _resolve_repo_names_to_ids(conn, ["pics"], load_config(cfg)) == [repo_id]
    conn.close()
    capsys.readouterr()
    assert main(["--config", str(cfg), "stage", "--repo", "pics", "--dry-run",
                 "--allow-escrow-drift"]) == 0
    out = capsys.readouterr().out
    assert "not registered" not in out
    assert "[DRY RUN] 1 volume(s) planned" in out


# --------------------------------------------------------------------------
# #683: no mixing of two splits in one folder
# --------------------------------------------------------------------------


def test_second_split_into_a_folder_with_old_cards_is_refused(tmp_path, capsys):
    pw = _write_pw_file(tmp_path / "alpha.pw")
    out = tmp_path / "cards"
    base = ["key", "split", "--repo", "alpha", "--password-file", str(pw),
            "--no-verify-repo", "--out", str(out), "-k", "3"]
    assert main([*base, "-n", "5"]) == 0
    before = sorted(p.name for p in out.iterdir())
    capsys.readouterr()

    assert main([*base[:-1], "2", "-n", "3"]) == 1

    msg = capsys.readouterr().out
    assert "alpha-share-4.txt" in msg and "alpha-share-5-card.txt" in msg
    assert "No shares were written" in msg
    assert sorted(p.name for p in out.iterdir()) == before  # untouched


def test_other_repositorys_cards_do_not_block_a_split(tmp_path):
    pw = _write_pw_file(tmp_path / "alpha.pw")
    out = tmp_path / "cards"
    out.mkdir()
    (out / "beta-share-1.txt").write_text("x\n")
    assert main(["key", "split", "--repo", "alpha", "--password-file", str(pw),
                 "--no-verify-repo", "--out", str(out), "-k", "2", "-n", "3"]) == 0


# --------------------------------------------------------------------------
# #681: key unsplit
# --------------------------------------------------------------------------


def _split_recorded(tmp_path: Path) -> tuple[Path, Path, str]:
    cfg, db, repo_id = _renamed_setup(tmp_path)
    conn = get_connection(db)
    record_split(conn, repo_id, 2, 3, 12345)
    conn.close()
    return cfg, db, repo_id


def test_unsplit_clears_the_record_and_writes_an_audit_line(tmp_path, capsys):
    cfg, db, repo_id = _split_recorded(tmp_path)
    capsys.readouterr()

    assert main(["--config", str(cfg), "key", "unsplit", "--repo", "pics",
                 "--yes"]) == 0

    out = capsys.readouterr().out
    assert "AUDIT" in out and "key unsplit" in out and "2-of-3" in out
    assert repo_id in out
    conn = get_connection(db)
    assert get_split_for_repo(conn, repo_id) is None
    # With the record gone, key_split=false is consistent: no drift.
    assert detect_escrow_drift(
        conn, [repo_id], config_split=False, config_threshold=2,
        config_shares=5,
    ) is None
    conn.close()


def test_unsplit_asks_first(tmp_path, monkeypatch):
    cfg, db, repo_id = _split_recorded(tmp_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")

    assert main(["--config", str(cfg), "key", "unsplit", "--repo", "pics"]) == 0

    conn = get_connection(db)
    assert get_split_for_repo(conn, repo_id) is not None
    conn.close()


def test_unsplit_also_clears_a_record_kept_under_the_name(tmp_path):
    """Builds before #571 recorded the split under the repository name."""
    cfg, db, repo_id = _renamed_setup(tmp_path)
    conn = get_connection(db)
    record_split(conn, "photos", 2, 3, 1)
    conn.close()

    assert main(["--config", str(cfg), "key", "unsplit", "--repo", "photos",
                 "--yes"]) == 0

    conn = get_connection(db)
    assert get_split_for_repo(conn, repo_id) is None
    conn.close()


def test_unsplit_with_no_record_is_a_no_op(tmp_path, capsys):
    cfg, _db, _ = _renamed_setup(tmp_path)
    assert main(["--config", str(cfg), "key", "unsplit", "--repo", "pics",
                 "--yes"]) == 0
    assert "Nothing to clear" in capsys.readouterr().out


def test_drift_message_names_key_unsplit():
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "id-a", "photos", "/m/photos")
    record_split(conn, "id-a", 2, 3, 7)

    msg = detect_escrow_drift(
        conn, ["id-a"], config_split=False, config_threshold=2, config_shares=5,
    )

    assert msg is not None
    assert "key unsplit --repo photos" in msg
    assert "key combine" not in msg
    conn.close()
