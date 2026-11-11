"""A pack pruned by one scan is un-pruned when a later scan finds it (#667).

Prune-sync marks packs missing from the mirror as pruned.  A partly
unavailable network share looks the same, and registration skips known
SHAs, so such a pack used to stay pruned for good: never staged, and
``lcsas status`` reported nothing unarchived.  Also pins #696 item 9: the
"Unarchived" line is counted after prune-sync.
"""

from __future__ import annotations

from pathlib import Path

from lcsas.cli.main import main
from lcsas.db.connection import get_connection, get_memory_connection
from lcsas.db.packs import bulk_mark_pruned, get_pack_by_sha256, register_pack
from lcsas.db.queries import get_unarchived_packs
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.packs.delta import DeltaAnalyzer


def _sha(i: int) -> str:
    return f"{i:064x}"


def _put_pack(mirror: Path, sha: str) -> None:
    d = mirror / "data" / sha[:2]
    d.mkdir(parents=True, exist_ok=True)
    (d / sha).write_bytes(b"x" * 64)


def _setup(tmp_path: Path, on_disk: list[str]) -> tuple[Path, Path, Path]:
    mirror = tmp_path / "mirror" / "family"
    for sub in ("keys", "index", "snapshots", "data"):
        (mirror / sub).mkdir(parents=True, exist_ok=True)
    (mirror / "config").write_text("{}")
    for sha in on_disk:
        _put_pack(mirror, sha)
    (tmp_path / "staging").mkdir()
    db_path = tmp_path / "archive.db"
    config = tmp_path / "config.toml"
    config.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{db_path}"\n'
        '\n[defaults]\nmedia_type = "TEST_TINY"\nmetadata_reserve_mb = 0\n'
        f'\n[repos.family]\nmirror_path = "{mirror}"\n'
    )
    main(["init", "--db-path", str(db_path)])
    conn = get_connection(db_path)
    create_all(conn)
    register_repo(conn, "family-id", "family", str(mirror), "")
    conn.close()
    return mirror, config, db_path


def _scan(config: Path, db_path: Path) -> int:
    return main(["--config", str(config), "--db", str(db_path),
                 "scan", "--no-snapshots"])


def test_absent_then_present_pack_is_unpruned_and_stageable(tmp_path, capsys):
    shas = [_sha(i) for i in range(1, 21)]
    mirror, config, db_path = _setup(tmp_path, on_disk=shas)

    assert _scan(config, db_path) == 0          # registers all 20
    capsys.readouterr()

    # Scan 1: two packs are missing (a share that was briefly unreachable).
    gone = shas[:2]
    for sha in gone:
        (mirror / "data" / sha[:2] / sha).unlink()
    assert _scan(config, db_path) == 0
    out = capsys.readouterr().out
    assert "Pruned packs:   2" in out
    # #696 item 9: counted after prune-sync, not before.
    assert "Unarchived:     18 " in out

    # Scan 2: they are back.
    for sha in gone:
        _put_pack(mirror, sha)
    assert _scan(config, db_path) == 0
    out = capsys.readouterr().out
    assert "Un-pruned:      2" in out
    assert "Unarchived:     20 " in out

    conn = get_connection(db_path)
    try:
        for sha in gone:
            pack = get_pack_by_sha256(conn, sha)
            assert pack is not None and not pack.is_pruned
        # The default `lcsas stage` pool.
        pool = {p.sha256 for p in get_unarchived_packs(conn)}
        assert set(gone) <= pool
    finally:
        conn.close()


def test_unprune_found_touches_only_this_repository_and_present_packs():
    conn = get_memory_connection()
    create_all(conn)
    register_repo(conn, "a", "a", "/m/a")
    register_repo(conn, "b", "b", "/m/b")
    a_back = register_pack(conn, sha256=_sha(1), size_bytes=64, repo_id="a")
    a_gone = register_pack(conn, sha256=_sha(2), size_bytes=64, repo_id="a")
    b_pack = register_pack(conn, sha256=_sha(3), size_bytes=64, repo_id="b")
    bulk_mark_pruned(conn, [a_back.pack_id, a_gone.pack_id, b_pack.pack_id])

    # The scan of repository a finds _sha(1) and (oddly) b's pack.
    found = DeltaAnalyzer(conn, {_sha(1): 64, _sha(3): 64}, "a").unprune_found()

    assert [p.sha256 for p in found] == [_sha(1)]
    assert not get_pack_by_sha256(conn, _sha(1)).is_pruned
    assert get_pack_by_sha256(conn, _sha(2)).is_pruned
    assert get_pack_by_sha256(conn, _sha(3)).is_pruned
    conn.close()
