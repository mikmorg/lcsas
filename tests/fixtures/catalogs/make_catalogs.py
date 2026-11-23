"""Make the disc catalogs in this folder (#961).  Run by hand, once per
release; never by a test.

    PYTHONPATH=src python3 -m tests.fixtures.catalogs.make_catalogs \\
        --release v1.0 [--work DIR]

Each release adds its own disc catalog and keeps the older ones, so the
gate reads every release's disc catalog with the newest readers
(``tests/recovery_hardening/test_catalogs_across_releases.py``).

``--release`` makes ``<release>.db.gz``: the disc catalog of the newest
disc of a real scratch install (real rustic, real xorriso, every owner
command through ``lcsas.cli.main.main``), staged in two sessions, the
first one recorded as burned.  Beside it go that disc's metadata of the
backup set ``alpha`` (``repo/<repo_id>/``: config, keys, index,
snapshots; no packs) and its password, so a reader can be run on the
set with every pack missing, and ``<release>.json``: the labels, the
backup set and the snapshots.

``--next`` makes the synthetic next-version catalogs from a release's
one: what a v11 may do and every older reader must live with.  Each is
the release catalog with a row 11 appended to ``schema_version`` and
one change; a CHECK is widened the way a real migration would
(the table's DDL changed, the rows kept).

The catalogs are gzipped, with no file time: a disc catalog is mostly
empty pages.  ``--next`` remakes the same bytes from the same release
catalog; ``--release`` makes a new install each time (new ids, packs
and times), so a release's catalog is made once, at its tag, and kept.
When a v11 is real, the row 11 here (``NEXT_ROW``) and the version the
test expects of the next-version catalogs become 12.
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tests.integration.scratch_install import ScratchInstall

HERE = Path(__file__).resolve().parent

#: name -> (what it is, SQL run on a copy of the release catalog).  Each
#: also gets the row 11 (``NEXT_ROW``) but the ``migrated`` one.
#: Every time a next-version change writes is given, so ``--next`` remakes
#: the same bytes from the same release catalog.
NEXT_ROW = ("INSERT INTO schema_version (version, applied_at) "
            "VALUES (11, '2030-01-01 00:00:00');")
NEXT: dict[str, tuple[str, str]] = {
    "migrated": (
        "this release's catalog as a master migrated from v5 leaves it: "
        "a row for each version it went through, oldest first",
        "DELETE FROM schema_version;"
        "INSERT INTO schema_version (version, applied_at) VALUES "
        "(5, '2026-03-01 00:00:00'), (6, '2026-04-01 00:00:00'), "
        "(7, '2026-05-01 00:00:00'), (8, '2026-06-01 00:00:00'), "
        "(9, '2026-07-01 00:00:00'), (10, '2026-08-01 00:00:00');",
    ),
    "next-row": ("a row 11 appended, nothing else", ""),
    "next-columns": (
        "extra nullable columns on volumes and packs, with values",
        "ALTER TABLE volumes ADD COLUMN future_note TEXT;"
        "ALTER TABLE packs ADD COLUMN future_tier INTEGER;"
        "UPDATE volumes SET future_note = 'from v11';"
        "UPDATE packs SET future_tier = 3;",
    ),
    "next-table-index": (
        "an extra table, with a row, and extra indexes",
        "CREATE TABLE future_things (thing_id INTEGER PRIMARY KEY, "
        "volume_id INTEGER REFERENCES volumes (volume_id), what TEXT);"
        "INSERT INTO future_things (volume_id, what) VALUES (1, 'v11');"
        "CREATE INDEX idx_future_things_volume ON future_things (volume_id);"
        "CREATE INDEX idx_packs_size ON packs (size_bytes);",
    ),
    "next-volume-status": (
        "a volume status this release does not know, on the first volume",
        "@widen volumes|'DESTROYED'|'DESTROYED', 'SHELVED'\n"
        "UPDATE volumes SET status = 'SHELVED' WHERE volume_id = 1;",
    ),
    "next-disc-status": (
        "a disc status (volume_copies) this release does not know",
        "@widen volume_copies|'DESTROYED'|'DESTROYED', 'LENT'\n"
        "UPDATE volume_copies SET status = 'LENT';",
    ),
    "next-media-type": (
        "a media type this release does not know, on the first volume",
        "UPDATE volumes SET media_type = 'BDXL128' WHERE volume_id = 1;",
    ),
    "next-event-type": (
        "a volume event type this release does not know",
        "@widen volume_events|'NOTE'|'NOTE', 'RELABELLED'\n"
        "INSERT INTO volume_events (volume_id, event_type, event_date, "
        "detail) VALUES (1, 'RELABELLED', '2030-01-01T00:00:00.000+00:00', "
        "'from v11');",
    ),
}


def _gzip_to(src: Path, dst: Path) -> None:
    with open(src, "rb") as f, open(dst, "wb") as raw, \
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        shutil.copyfileobj(f, gz)


def _apply(db: Path, script: str) -> None:
    """Run *script* on *db*.  A line ``@widen <table>|<old>|<new>``
    replaces *old* by *new* in the table's CREATE (its CHECK), as a real
    migration would by recreating the table; the rows are kept."""
    conn = sqlite3.connect(db)
    try:
        sql = []
        for line in script.split("\n"):
            if line.startswith("@widen "):
                table, old, new = line[len("@widen "):].split("|")
                (ddl,) = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type = 'table' "
                    "AND name = ?", (table,)).fetchone()
                assert old in ddl, (table, old)
                version = conn.execute("PRAGMA schema_version").fetchone()[0]
                conn.execute("PRAGMA writable_schema = ON")
                conn.execute(
                    "UPDATE sqlite_master SET sql = ? WHERE type = 'table' "
                    "AND name = ?", (ddl.replace(old, new, 1), table))
                conn.execute(f"PRAGMA schema_version = {version + 1}")
                conn.execute("PRAGMA writable_schema = OFF")
                conn.commit()
            else:
                sql.append(line)
        conn.executescript("\n".join(sql))
        conn.commit()
    finally:
        conn.close()
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        conn.close()


def _backup(inst: ScratchInstall, name: str, filename: str) -> None:
    """``inst.backup``, but with relative paths and a made-up host name:
    a snapshot records its command line and host, and the snapshot
    files are committed (the repository is public)."""
    (inst.data(name) / filename).write_bytes(os.urandom(6000))
    subprocess.run(
        ["rustic", "-r", f"repos/{name}", "--password-file",
         f"keys/{name}.key", "backup", f"data/{name}", "--as-path",
         f"/{name}", "--host", "lcsas-fixture"],
        cwd=inst.root, capture_output=True, check=True,
        env={**os.environ, "TMPDIR": str(inst.root / "tmp"),
             "HOME": str(inst.root / "tmp")})


def make_release(release: str, work: Path) -> None:
    from tests.integration.scratch_install import ScratchInstall

    inst = ScratchInstall(work / "install", repos=("alpha", "beta"))
    _backup(inst, "alpha", "a1.bin")
    (snap1,) = inst.mirror_snapshots("alpha")
    _backup(inst, "beta", "b1.bin")
    inst.add_repos()
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0
    (first,) = inst.sessions()
    inst.mark_burned(first)
    _backup(inst, "alpha", "a2.bin")
    (snap2,) = inst.mirror_snapshots("alpha") - {snap1}
    assert inst.lcsas("scan") == 0
    assert inst.lcsas("stage") == 0

    trees = inst.disc_trees()
    newest = max(trees)
    disc = trees[newest]
    repo_id = inst.repo_id("alpha")
    _gzip_to(disc / "catalog.db", HERE / f"{release}.db.gz")
    repo = HERE / "repo"
    if repo.exists():
        shutil.rmtree(repo)
    shutil.copytree(disc / "metadata" / repo_id, repo / repo_id)
    shutil.copy(inst.key("alpha"), HERE / "repo" / "password")
    info = json.loads((disc / "volume_info.json").read_text())
    (HERE / f"{release}.json").write_text(json.dumps({
        "disc": {"label": info["label"], "uuid": info["uuid"]},
        "labels": sorted(trees),
        "repo_id": repo_id,
        "snapshots": [snap1, snap2],
    }, indent=2) + "\n")


def make_next(release: str, work: Path) -> None:
    src = work / "release.db"
    with gzip.open(HERE / f"{release}.db.gz", "rb") as f:
        src.write_bytes(f.read())
    for name, (_what, script) in NEXT.items():
        db = work / f"{name}.db"
        shutil.copy(src, db)
        _apply(db, script if name == "migrated" else NEXT_ROW + "\n" + script)
        _gzip_to(db, HERE / f"{release}-{name}.db.gz")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--release", help="make this release's catalog")
    ap.add_argument("--next", metavar="RELEASE",
                    help="make the next-version catalogs from RELEASE's")
    ap.add_argument("--work", type=Path)
    args = ap.parse_args()
    with tempfile.TemporaryDirectory(dir=args.work) as tmp:
        if args.release:
            make_release(args.release, Path(tmp) / "r")
        if args.next:
            (Path(tmp) / "n").mkdir()
            make_next(args.next, Path(tmp) / "n")


if __name__ == "__main__":
    main()
