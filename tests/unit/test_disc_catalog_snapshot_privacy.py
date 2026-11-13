"""#733 -- a disc catalog does not carry snapshot source folders, tags or
descriptions.

Owner decision (2026-11-13): every disc may carry the whole catalog, but a
snapshot's source folders, tags and description must not be readable from
a disc without the password.  Every restore tier reads them from rustic's
own (encrypted) snapshot files, so ``inject_catalog`` blanks the catalog's
plaintext copy on the disc.  These tests pin:

1. the disc catalog has the blank value in every blanked column of every
   snapshot row, and the old text is not anywhere in the file's bytes;
2. the owner's master catalog keeps the values;
3. ``catalog rebuild`` from such discs works, and fills a blanked row from
   an older disc that still has the values;
4. a later ``lcsas scan`` writes paths and tags again from rustic.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from unittest.mock import patch

from lcsas.cli.main import main
from lcsas.db.connection import get_connection
from lcsas.db.rebuild import rebuild_catalog
from lcsas.db.repos import register_repo
from lcsas.db.schema import create_all
from lcsas.db.snapshots import (
    DISC_BLANKED_SNAPSHOT_COLUMNS,
    list_snapshots,
    upsert_snapshot,
)
from lcsas.rustic.types import SnapshotInfo
from lcsas.staging.metadata import HolographicInjector
from lcsas.utils.fs import copy_file

MARK_PATH = "/srv/zz-private-folder-733"
MARK_TAG = "zz-private-tag-733"
MARK_DESC = "zz-private-description-733"
DELETED_MARK = "zz-deleted-row-733"
MARKERS = (b"zz-private-folder-733", b"zz-private-tag-733",
           b"zz-private-description-733")


def _master(tmp_path: Path, mirror: Path) -> Path:
    """A WAL master catalog with two snapshots carrying private values."""
    db = tmp_path / "master" / "archive.db"
    db.parent.mkdir()
    conn = get_connection(db)
    # Debian/Ubuntu build SQLite with secure_delete on; most others do not.
    # Off, freed space keeps its old text, as on those platforms.
    conn.execute("PRAGMA secure_delete=OFF")
    create_all(conn)
    register_repo(conn, "r1", "family", str(mirror), "")
    # An earlier version of s1, replaced: SQLite leaves the old record in
    # the file's free space unless the copy is rebuilt.
    upsert_snapshot(conn, "s1", "r1", hostname="nas",
                    timestamp="2026-01-01T00:00:00Z",
                    paths=json.dumps([MARK_PATH + "-old"]),
                    tags=json.dumps([MARK_TAG + "-old"]),
                    description=MARK_DESC + "-old")
    upsert_snapshot(conn, "s1", "r1", hostname="nas",
                    timestamp="2026-01-01T00:00:00Z",
                    paths=json.dumps([MARK_PATH]),
                    tags=json.dumps([MARK_TAG]),
                    description=MARK_DESC)
    # A long value spills onto overflow pages; an UPDATE alone would put
    # them on the free list with the text still in them.
    upsert_snapshot(conn, "s3", "r1", hostname="nas",
                    timestamp="2026-01-03T00:00:00Z",
                    paths=json.dumps([MARK_PATH + "/" + "x" * 8000]),
                    tags="[]", description=MARK_DESC * 300)
    upsert_snapshot(conn, "s2", "r1", hostname="nas",
                    timestamp="2026-01-02T00:00:00Z",
                    paths=json.dumps([MARK_PATH + "/two"]),
                    tags="[]", description="")
    # A snapshot row deleted from the master: its text stays on the free
    # list of the master file, and a byte copy carries it.
    upsert_snapshot(conn, "s0", "r1", description=DELETED_MARK * 400)
    conn.execute("DELETE FROM snapshots WHERE snapshot_id = 's0'")
    conn.commit()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return db


def _rows(db: Path) -> dict[str, tuple[str, str, str, str, str]]:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return {
            r[0]: tuple(r[1:])
            for r in conn.execute(
                "SELECT snapshot_id, hostname, timestamp, paths, tags, "
                "description FROM snapshots"
            )
        }
    finally:
        conn.close()


def _disc(tmp_path: Path, master: Path, name: str) -> Path:
    disc = tmp_path / name
    disc.mkdir()
    HolographicInjector(disc).inject_catalog(master)
    return disc


def test_blanked_columns_constant() -> None:
    """The three private columns, each with its schema "none" value.

    hostname stays (the owner may add it later as one more entry).
    """
    assert DISC_BLANKED_SNAPSHOT_COLUMNS == {
        "paths": "[]", "tags": "[]", "description": "",
    }


def test_disc_catalog_blanks_snapshot_columns(tmp_path: Path) -> None:
    master = _master(tmp_path, tmp_path / "mirror")
    raw = master.read_bytes()
    assert all(m in raw for m in MARKERS)
    # The deleted row is still in the master's free space.
    assert DELETED_MARK.encode() in raw

    disc = _disc(tmp_path, master, "disc")
    dst = disc / "catalog.db"

    rows = _rows(dst)
    assert sorted(rows) == ["s1", "s2", "s3"]
    for hostname, timestamp, paths, tags, description in rows.values():
        assert hostname == "nas"  # not blanked
        assert timestamp.startswith("2026-01-0")
        assert (paths, tags, description) == ("[]", "[]", "")

    # Not in a free page or a cell's leftover space either.
    raw = dst.read_bytes()
    for marker in (*MARKERS, DELETED_MARK.encode()):
        assert marker not in raw, marker

    # Still a rollback-journal file with no sidecar (#534).
    assert raw[18:20] == b"\x01\x01"
    assert sorted(p.name for p in disc.iterdir()) == ["catalog.db"]

    # The owner's master catalog keeps the values.
    kept = _rows(master)
    assert json.loads(kept["s1"][2]) == [MARK_PATH]
    assert json.loads(kept["s1"][3]) == [MARK_TAG]
    assert kept["s1"][4] == MARK_DESC
    assert json.loads(kept["s2"][2]) == [MARK_PATH + "/two"]


def test_rebuild_from_blanked_disc_then_scan_refills(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror" / "family"
    for sub in ("data", "keys", "index", "snapshots"):
        (mirror / sub).mkdir(parents=True)
    (mirror / "config").write_text("{}")
    master = _master(tmp_path, mirror)
    disc = _disc(tmp_path, master, "disc")

    rebuilt = tmp_path / "rebuilt.db"
    result = rebuild_catalog([disc], rebuilt)
    assert result.ok, result.errors
    assert result.snapshots_merged == 3
    rows = _rows(rebuilt)
    assert {v[2:] for v in rows.values()} == {("[]", "[]", "")}

    # A later scan lists the snapshots with rustic and writes them again.
    pw = tmp_path / "family.pw"
    pw.write_text("hunter2\n")
    cfg = tmp_path / "config.toml"
    cfg.write_text(
        f'[paths]\nmirror_base = "{tmp_path / "mirror"}"\n'
        f'staging = "{tmp_path / "staging"}"\n'
        f'database = "{rebuilt}"\n'
        "\n[defaults]\n"
        'media_type = "TEST_TINY"\n'
        "ecc_redundancy_pct = 0\n"
        'location = "Home_Shelf"\n'
        'optical_device = "/dev/null"\n'
        'label_prefix = "TEST"\n'
        "metadata_reserve_mb = 0\n"
        f'\n[repos.family]\nmirror_path = "{mirror}"\n'
        f'password_file = "{pw}"\n'
    )
    snaps = [
        SnapshotInfo(snapshot_id="s1", timestamp="2026-01-01T00:00:00Z",
                     hostname="nas", paths=[MARK_PATH], tags=[MARK_TAG]),
        SnapshotInfo(snapshot_id="s2", timestamp="2026-01-02T00:00:00Z",
                     hostname="nas", paths=[MARK_PATH + "/two"], tags=[]),
    ]
    with patch("lcsas.rustic.wrapper.SubprocessRusticRunner") as runner_cls, \
            patch("lcsas.utils.subprocess.check_binary_version"):
        runner_cls.return_value.snapshots.return_value = snaps
        assert main(["--config", str(cfg), "--db", str(rebuilt), "scan"]) == 0

    conn = get_connection(rebuilt)
    try:
        got = {s.snapshot_id: s for s in list_snapshots(conn)}
    finally:
        conn.close()
    assert json.loads(got["s1"].paths) == [MARK_PATH]
    assert json.loads(got["s1"].tags) == [MARK_TAG]
    assert json.loads(got["s2"].paths) == [MARK_PATH + "/two"]


def test_rebuild_fills_blanked_rows_from_an_older_disc(tmp_path: Path) -> None:
    """A newer blanked disc merges first; an older full disc fills it in."""
    master = _master(tmp_path, tmp_path / "mirror")
    new_disc = _disc(tmp_path, master, "new_disc")
    # A disc staged before #733: a plain copy of the catalog.
    old_disc = tmp_path / "old_disc"
    copy_file(master, old_disc / "catalog.db")
    os.utime(old_disc / "catalog.db", (1_000_000, 1_000_000))
    os.utime(new_disc / "catalog.db", (2_000_000, 2_000_000))

    rebuilt = tmp_path / "rebuilt.db"
    result = rebuild_catalog([new_disc, old_disc], rebuilt)
    assert result.ok, result.errors
    rows = _rows(rebuilt)
    assert json.loads(rows["s1"][2]) == [MARK_PATH]
    assert json.loads(rows["s1"][3]) == [MARK_TAG]
    assert rows["s1"][4] == MARK_DESC
    assert json.loads(rows["s2"][2]) == [MARK_PATH + "/two"]

    # Only the new disc: the rows stay blank (nothing to fill from).
    only_new = tmp_path / "only_new.db"
    assert rebuild_catalog([new_disc], only_new).ok
    assert _rows(only_new)["s1"][2:] == ("[]", "[]", "")


def test_rebuild_keeps_values_already_in_the_output(tmp_path: Path) -> None:
    """A filled row in the output catalog is never overwritten by a disc."""
    master = _master(tmp_path, tmp_path / "mirror")
    old_disc = tmp_path / "old_disc"
    copy_file(master, old_disc / "catalog.db")
    rebuilt = tmp_path / "rebuilt.db"
    copy_file(master, rebuilt)
    conn = sqlite3.connect(rebuilt)
    conn.execute("UPDATE snapshots SET paths = '[\"/kept\"]' "
                 "WHERE snapshot_id = 's1'")
    conn.commit()
    conn.close()
    assert rebuild_catalog([old_disc], rebuilt).ok
    assert json.loads(_rows(rebuilt)["s1"][2]) == ["/kept"]
