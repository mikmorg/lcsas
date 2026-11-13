"""#657 -- only snapshots whose packs are all on discs go onto a disc.

A disc carried every snapshot file of the mirror, including snapshots
whose packs were never burned.  "latest" then picked such a snapshot and
the restore asked for a disc that does not exist.  These tests pin:

1. ``PurePythonRestorer.missing_packs_by_snapshot`` finds, per snapshot,
   the packs it needs that are not held (real rustic repository).
2. ``snapshot_coverage`` splits the snapshots into complete / left out.
3. ``HolographicInjector.inject_metadata(snapshot_allow=...)`` copies only
   the allowed snapshot files and keeps the folder when none is allowed.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.db.connection import get_connection
from lcsas.db.models import Snapshot
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.snapshots import bulk_upsert_snapshots
from lcsas.packs.snapshot_coverage import snapshot_coverage
from lcsas.restore.restic_fallback import PurePythonRestorer
from lcsas.rustic.wrapper import SubprocessRusticRunner
from lcsas.staging.metadata import HolographicInjector

pytestmark = [pytest.mark.integration, pytest.mark.requires_rustic]


def _rustic(repo: Path, pw: Path, *args: str, tmp: Path) -> None:
    subprocess.run(
        ["rustic", "-r", str(repo), "--password-file", str(pw), *args],
        capture_output=True, text=True, check=True,
        env={**os.environ, "TMPDIR": str(tmp)},
    )


def _packs(repo: Path) -> set[str]:
    return {p.name for p in (repo / "data").rglob("*") if p.is_file()}


@pytest.fixture
def two_snapshots(tmp_path: Path) -> tuple[Path, Path, set[str], set[str]]:
    """A repo with two snapshots; returns (repo, pw, packs after 1, after 2)."""
    repo = tmp_path / "repo"
    pw = tmp_path / "pw"
    pw.write_text("coverage-test\n")
    src = tmp_path / "src"
    src.mkdir()
    (src / "one.bin").write_bytes(random.Random(1).randbytes(8192))
    _rustic(repo, pw, "init", tmp=tmp_path)
    _rustic(repo, pw, "backup", str(src), tmp=tmp_path)
    first = _packs(repo)
    (src / "two.bin").write_bytes(random.Random(2).randbytes(8192))
    _rustic(repo, pw, "backup", str(src), tmp=tmp_path)
    second = _packs(repo)
    assert second > first, "second backup must add packs"
    return repo, pw, first, second


def test_missing_packs_by_snapshot(
    two_snapshots: tuple[Path, Path, set[str], set[str]],
) -> None:
    repo, pw, first, second = two_snapshots
    r = PurePythonRestorer(repo, password_file=pw, interactive=False,
                           strict=True)

    # Only the first backup's packs are held: snapshot 1 complete,
    # snapshot 2 needs exactly packs written by the second backup.
    result = r.missing_packs_by_snapshot(first)
    assert len(result) == 2
    (s1, m1), (s2, m2) = result
    assert s1.time < s2.time
    assert m1 == frozenset()
    assert m2 and m2 <= (second - first)

    # Everything held: both complete.
    assert all(not m for _s, m in r.missing_packs_by_snapshot(second))

    # Nothing held: both incomplete.
    assert all(m for _s, m in r.missing_packs_by_snapshot(set()))


def test_snapshot_coverage_split(
    two_snapshots: tuple[Path, Path, set[str], set[str]],
) -> None:
    repo, pw, first, _second = two_snapshots
    cov = snapshot_coverage(repo, pw, first)
    snaps = sorted(p.name for p in (repo / "snapshots").iterdir())
    assert len(cov.complete) == 1
    assert len(cov.left_out) == 1
    assert cov.complete | {cov.left_out[0].snapshot_id} == set(snaps)
    assert cov.left_out[0].missing_packs >= 1


def test_unreadable_tree_is_never_complete(
    two_snapshots: tuple[Path, Path, set[str], set[str]],
) -> None:
    """A tree that cannot be read makes its snapshot incomplete."""
    repo, pw, _first, second = two_snapshots
    r = PurePythonRestorer(repo, password_file=pw, interactive=False,
                           strict=True)
    # Damage every pack: tree blobs no longer authenticate.
    for pack in (repo / "data").rglob("*"):
        if pack.is_file():
            data = bytearray(pack.read_bytes())
            data[len(data) // 2] ^= 0xFF
            pack.write_bytes(bytes(data))
    result = r.missing_packs_by_snapshot(second)
    assert all(any(x.startswith("?tree:") for x in m) for _s, m in result)


def test_inject_metadata_filters_snapshots(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror"
    for d in ("index", "snapshots", "keys"):
        (mirror / d).mkdir(parents=True)
    (mirror / "config").write_text("c")
    (mirror / "keys" / "k1").write_text("k")
    (mirror / "index" / "i1").write_text("i")
    for name in ("aaa", "bbb", "ccc"):
        (mirror / "snapshots" / name).write_text(name)

    staging = tmp_path / "staging"
    inj = HolographicInjector(staging)
    inj.inject_metadata({"r1": mirror, "r2": mirror},
                        snapshot_allow={"r1": {"aaa", "ccc"}, "r2": set()})
    got1 = sorted(p.name for p in (staging / "metadata/r1/snapshots").iterdir())
    assert got1 == ["aaa", "ccc"]
    # No snapshot allowed: the folder is kept, empty.
    assert (staging / "metadata/r2/snapshots").is_dir()
    assert list((staging / "metadata/r2/snapshots").iterdir()) == []
    # Other metadata is copied as before.
    assert (staging / "metadata/r1/index/i1").is_file()

    # No filter: every snapshot file.
    staging2 = tmp_path / "staging2"
    HolographicInjector(staging2).inject_metadata({"r1": mirror})
    got = sorted(p.name for p in (staging2 / "metadata/r1/snapshots").iterdir())
    assert got == ["aaa", "bbb", "ccc"]


def test_disc_catalog_blank_but_reader_lists_snapshot_paths(
    tmp_path: Path,
) -> None:
    """#733: the disc catalog has no source folders, tags or description.

    The master catalog holds them as ``lcsas scan`` wrote them from
    rustic; the disc catalog holds them blank; the snapshot files are
    still chosen by id (#657); and the tier-3 reader still lists the
    snapshot's folders and tags from the disc's encrypted snapshot file.
    """
    repo = tmp_path / "repo"
    pw = tmp_path / "pw"
    pw.write_text("privacy-test\n")
    src = tmp_path / "zz-private-folder-733"
    src.mkdir()
    (src / "a.bin").write_bytes(random.Random(3).randbytes(4096))
    _rustic(repo, pw, "init", tmp=tmp_path)
    _rustic(repo, pw, "backup", "--host", "fixture-host",
            "--tag", "zz-private-tag-733",
            "--description", "zz-private-description-733",
            str(src), tmp=tmp_path)

    # The master catalog, as scan fills it from rustic.
    master = tmp_path / "archive.db"
    conn = get_connection(master)
    create_all(conn)
    register_repo(conn, "r1", "family", str(repo), "")
    infos = SubprocessRusticRunner(tmpdir=tmp_path).snapshots(repo, pw)
    bulk_upsert_snapshots(conn, [
        Snapshot(snapshot_id=si.snapshot_id, repo_id="r1",
                 hostname=si.hostname, timestamp=si.timestamp,
                 paths=json.dumps(si.paths), tags=json.dumps(si.tags),
                 description="zz-private-description-733")
        for si in infos
    ])
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()

    cov = snapshot_coverage(repo, pw, _packs(repo))
    ids = {p.name for p in (repo / "snapshots").iterdir()}
    assert cov.complete == ids
    assert {si.snapshot_id for si in infos} == ids

    staging = tmp_path / "staging"
    inj = HolographicInjector(staging)
    inj.inject_metadata({"r1": repo}, required_repos={"r1"},
                        snapshot_allow={"r1": cov.complete})
    inj.inject_catalog(master)

    def rows(db: Path) -> list[tuple[str, str, str, str, str]]:
        c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return c.execute(
                "SELECT snapshot_id, hostname, paths, tags, description "
                "FROM snapshots").fetchall()
        finally:
            c.close()

    ((sid, host, paths, tags, desc),) = rows(master)
    assert sid in ids and host == "fixture-host"
    assert json.loads(paths) == [str(src)]
    assert json.loads(tags) == ["zz-private-tag-733"]
    assert desc == "zz-private-description-733"

    disc_catalog = staging / "catalog.db"
    assert rows(disc_catalog) == [(sid, "fixture-host", "[]", "[]", "")]
    assert b"zz-private" not in disc_catalog.read_bytes()

    meta = staging / "metadata" / "r1"
    assert {p.name for p in (meta / "snapshots").iterdir()} == ids
    reader = PurePythonRestorer(meta, password_file=pw, interactive=False)
    (snap,) = reader.list_snapshots()
    assert snap.snapshot_id == sid
    assert snap.paths == [str(src)]
    assert snap.tags == ["zz-private-tag-733"]
