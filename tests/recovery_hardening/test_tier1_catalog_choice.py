"""Tier 1 and a disc of another archive (#950).

A disc of another archive, put in by mistake at a disc swap, is not this
restore's: its catalog does not know the backup set being restored (no
pack of the set's id, the name of the ``--repo`` folder).  Tier 1 used to
copy every pack on it into the pack cache as soon as it saw the disc
("drain on first sight"), and to take its catalog when its file was the
newest.  Now the drain skips it, with one line saying it belongs to
another archive, and a disc of this archive is still copied.

Runs the tier-1 program (``LCSAS_RESTORE_BIN``, else ``recovery/build``,
else the committed binary) on a repository written by
``tests/unit/tier3_repo_builder.py``; no rustic needed.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.recovery_hardening._diff_helpers import find_restore_bin

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="the tier-1 binary is POSIX")

SET_ID = "a1b2c3d4e5f60718"
FOREIGN_LINE = "belongs to another archive"


def _tier1() -> Path:
    env_bin = os.environ.get("LCSAS_RESTORE_BIN")
    found = Path(env_bin) if env_bin else find_restore_bin()
    if found is None or not found.is_file():
        pytest.skip("no lcsas-restore binary; run `make -C recovery`")
    return found


def _disc(root: Path, label: str, set_id: str,
          packs: dict[str, bytes]) -> None:
    """A data disc at *root*: its label, its packs under data/ and a
    catalog that puts them on it, in backup set *set_id*."""
    (root / "data").mkdir(parents=True)
    (root / "volume_info.json").write_text(json.dumps({"label": label}))
    conn = sqlite3.connect(root / "catalog.db")
    try:
        conn.executescript(
            "PRAGMA user_version = 4;"
            "CREATE TABLE schema_version (version INTEGER);"
            "INSERT INTO schema_version VALUES (10);"
            "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT,"
            "  size_bytes INTEGER, repo_id TEXT);"
            "CREATE TABLE volumes (volume_id INTEGER PRIMARY KEY,"
            "  label TEXT, status TEXT);"
            "CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER);")
        conn.execute("INSERT INTO volumes VALUES (1, ?, 'VERIFIED')", (label,))
        for n, (pack_id, data) in enumerate(sorted(packs.items()), start=1):
            (root / "data" / pack_id[:2]).mkdir(exist_ok=True)
            (root / "data" / pack_id[:2] / pack_id).write_bytes(data)
            conn.execute("INSERT INTO packs VALUES (?, ?, ?, ?)",
                         (n, pack_id, len(data), set_id))
            conn.execute("INSERT INTO volume_packs VALUES (1, ?)", (n,))
        conn.commit()
    finally:
        conn.close()


def test_a_disc_of_another_archive_is_not_copied_to_the_cache(
        tmp_path: Path) -> None:
    """At a swap, a disc of another archive is in a drive beside this
    archive's disc.  This archive's disc is copied into the pack cache
    and the restore completes; none of the other archive's packs reach
    the cache, and one line says why."""
    from tests.unit import tier3_repo_builder

    layout: dict[str, list[str]] = {}
    (tmp_path / "metadata").mkdir()
    repo = tier3_repo_builder.build_repo(
        tmp_path / "metadata", {"a": b"alpha\n", "b": b"beta\n"},
        name=SET_ID, pack_per_blob=True, layout=layout)
    wanted = layout["b"][0]
    ours = {wanted: (repo / "data" / wanted[:2] / wanted).read_bytes()}
    (repo / "data" / wanted[:2] / wanted).unlink()

    foreign: dict[str, bytes] = {}
    for n in range(3):
        data = f"another archive's pack {n}\n".encode() * 50
        foreign[hashlib.sha256(data).hexdigest()] = data
    mnt = tmp_path / "mnt"
    _disc(mnt / "OTHER", "LCSAS_OTHER_2026_0001", "another-set", foreign)
    _disc(mnt / "OURS", "LCSAS_OURS_2026_0001", SET_ID, ours)

    pwfile = tmp_path / "pw"
    pwfile.write_bytes(tier3_repo_builder.PASSWORD + b"\n")
    cache = tmp_path / "cache"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LCSAS_")}
    env["LCSAS_PACK_CACHE_DIR"] = str(cache)
    res = subprocess.run(
        [str(_tier1()), "--repo", str(repo), "--password-file", str(pwfile),
         "--restore-folder", str(tmp_path / "out"),
         "--mount-parent", str(mnt), "--interactive", "off"],
        capture_output=True, text=True, env=env, timeout=120)

    cached = {p.name for p in (cache / "data").rglob("*") if p.is_file()} \
        if (cache / "data").is_dir() else set()
    assert res.returncode == 0, res.stderr
    assert wanted in cached, res.stderr
    assert not cached & set(foreign), (
        f"another archive's packs in the cache: {sorted(cached & set(foreign))}"
        f"\n{res.stderr}")
    assert res.stderr.count(FOREIGN_LINE) == 1, res.stderr
    assert "LCSAS_OTHER_2026_0001" in [
        line for line in res.stderr.splitlines() if FOREIGN_LINE in line][0]
