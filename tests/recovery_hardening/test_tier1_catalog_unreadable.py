"""Hardening: tier 1 reads WAL catalogs off read-only media; unreadable isn't skew (#543).

Tier 1 reads a WAL catalog on read-only media, and doesn't blame a newer
LCSAS for a catalog it can't read.

#534 found discs whose catalog.db was a WAL-mode database.  SQLite opens
one read-only, but the first query then fails on a read-only volume:
reading WAL needs a -shm file beside the database, and SQLite can't
create it there.  The disc-swap prompt said "catalog could not be
queried -- likely written by a newer LCSAS ... use the restore tools
from the NEWEST meta disc", which sends an heir after a newer meta disc
when theirs was the right one.  #542 burns rollback-journal catalogs
now.  This is the reader side, for discs burned before it and for any
other read failure.

Each test runs the production binary with ``--interactive on``,
``--auto-mount off`` and "q" waiting on stdin, so it prints one
disc-swap prompt and stops:

  1. A real (schema v10) WAL catalog in a directory the binary can't
     write to, standing in for a read-only disc, names the missing
     pack's volume.
  2. A catalog that isn't a database says it couldn't be read, and why,
     with no word about a newer LCSAS.

The C side (recovery/tests/test_catalog.c, test_disc_locator.c) pins
the same branches function by function, plus a damaged page.
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from .test_tier1_rescan import BINARY, _build_split_fixture, _require_binary

_PROMPT = "is required for the next file"
_LABEL_B = "vol-WAL-B"


def _first_prompt(
    tmp: Path, repo: Path, pwfile: Path, disc_a: Path, catalog: Path
) -> str:
    """The binary's output up to its first disc-swap prompt, answered "q"."""
    target = tmp / "out"
    target.mkdir()
    run = subprocess.run(
        [
            str(BINARY),
            "--repo", str(repo),
            "--password-file", str(pwfile),
            "--target", str(target),
            "--snapshot", "latest",
            "--pack-search", str(disc_a),
            "--catalog", str(catalog),
            "--interactive", "on",
            "--auto-mount", "off",
        ],
        input="q\n", capture_output=True, text=True, timeout=120,
    )
    out = run.stdout + run.stderr
    assert _PROMPT in out, f"binary never prompted (rc {run.returncode}):\n{out}"
    return out


def _build_wal_catalog(path: Path, packs: list[tuple[str, int]]) -> None:
    """A schema-v10 catalog putting ``packs`` on _LABEL_B, in WAL mode."""
    from lcsas.db import schema as db_schema
    from lcsas.db.packs import register_pack
    from lcsas.db.volume_packs import bulk_link_packs
    from lcsas.db.volumes import create_volume

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    db_schema.create_all(conn)
    conn.execute(
        "INSERT OR IGNORE INTO repositories (repo_id, name, mirror_path) "
        "VALUES (?, ?, ?)",
        ("repo-wal", "wal", "/srv/wal"),
    )
    rows = [register_pack(conn, sha, size, "repo-wal") for sha, size in packs]
    vol = create_volume(
        conn, label=_LABEL_B, uuid="uuid-wal-b", media_type="BD25",
        capacity_bytes=26843545600, status="VERIFIED", commit=False,
    )
    bulk_link_packs(conn, vol.volume_id, [p.pack_id for p in rows], commit=False)
    conn.commit()
    assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    conn.close()


def test_wal_catalog_on_unwritable_media_names_the_volume(tmp_path: Path) -> None:
    _require_binary()
    if os.geteuid() == 0:
        pytest.skip("root can write to a 0555 directory: no read-only stand-in")
    repo, pwfile, disc_a, disc_b, _files = _build_split_fixture(tmp_path)
    disc = tmp_path / "disc"
    disc.mkdir()
    catalog = disc / "catalog.db"
    _build_wal_catalog(
        catalog,
        [(f.name, f.stat().st_size) for f in sorted((disc_b / "data").iterdir())],
    )
    assert [p.name for p in disc.iterdir()] == ["catalog.db"], (
        "the -wal and -shm files should be gone after close"
    )

    disc.chmod(0o555)
    try:
        out = _first_prompt(tmp_path, repo, pwfile, disc_a, catalog)
    finally:
        disc.chmod(0o755)
    assert "could not be" not in out, f"the WAL catalog was not read:\n{out}"
    assert "It lives on volume(s):" in out and _LABEL_B in out, (
        f"the prompt did not name the volume from the catalog:\n{out}"
    )


def test_unreadable_catalog_is_not_blamed_on_a_newer_lcsas(tmp_path: Path) -> None:
    _require_binary()
    repo, pwfile, disc_a, _disc_b, _files = _build_split_fixture(tmp_path)
    catalog = tmp_path / "catalog.db"
    catalog.write_bytes(b"not an SQLite database\n" * 40)
    out = _first_prompt(tmp_path, repo, pwfile, disc_a, catalog)
    assert "catalog could not be read" in out, out
    assert "Reason: file is not a database" in out, out
    assert "newer LCSAS" not in out, f"an unreadable catalog was reported as skew:\n{out}"
