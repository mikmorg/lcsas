"""Integration: ``lcsas catalog validate --content`` on a real mastered ISO.

Masters an ISO (via the production ``SubprocessXorrisoRunner``) from a
disc tree containing one intact and one bit-rotted pack, extracts it
back out with osirrox, and asserts the CLI exits non-zero naming the
corrupt pack (BURN-02).  Name-only validation must still pass on the
same disc — the corruption is invisible without the content hash.

Requires: ``xorriso`` on PATH.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from pathlib import Path

import pytest

from lcsas.cli.main import main
from lcsas.iso.xorriso import SubprocessXorrisoRunner

requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"),
    reason="xorriso not installed",
)


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_minimal_catalog(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE packs (pack_id INTEGER PRIMARY KEY, sha256 TEXT UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE volumes "
        "(volume_id INTEGER PRIMARY KEY, label TEXT, status TEXT)"
    )
    conn.execute("CREATE TABLE volume_packs (volume_id INTEGER, pack_id INTEGER)")
    conn.commit()
    conn.close()


@requires_xorriso
def test_catalog_validate_content_flags_corrupt_pack_on_real_iso(tmp_path, capsys):
    good_content = b"intact pack payload " * 64
    good_sha = _sha(good_content)
    bad_content = b"bit-rotted pack payload " * 64
    # The corrupt pack carries the hash of what it SHOULD contain.
    bad_sha = _sha(b"the original payload this pack should hold")

    # --- Build the disc tree (two-level pack layout, like staging) ---
    tree = tmp_path / "staging"
    for sha, content in ((good_sha, good_content), (bad_sha, bad_content)):
        subdir = tree / "data" / sha[:2]
        subdir.mkdir(parents=True, exist_ok=True)
        (subdir / sha).write_bytes(content)
    _write_minimal_catalog(tree / "catalog.db")
    (tree / "volume_info.json").write_text(
        json.dumps({
            "label": "BURN02_TEST",
            "sha256_manifest": [good_sha, bad_sha],
        })
    )

    # --- Master a real ISO with the production runner, extract it back ---
    iso = tmp_path / "volume.iso"
    runner = SubprocessXorrisoRunner(tmpdir=tmp_path)
    runner.create_iso(tree, iso, "BURN02TEST")
    assert iso.is_file() and iso.stat().st_size > 0

    extracted = tmp_path / "extracted"
    subprocess.run(
        [
            "xorriso", "-osirrox", "on",
            "-indev", str(iso),
            "-extract", "/", str(extracted),
        ],
        check=True,
        capture_output=True,
    )
    assert (extracted / "catalog.db").is_file()

    # --- Name-only validation passes: both filenames exist on disc ---
    assert main(["catalog", "validate", str(extracted)]) == 0
    capsys.readouterr()

    # --- Content mode exits non-zero, naming the corrupt pack ---
    rc = main(["catalog", "validate", str(extracted), "--content"])
    out = capsys.readouterr().out
    assert rc != 0
    assert bad_sha in out
    assert "CORRUPT" in out
    # The intact pack is not flagged.
    assert f"CORRUPT: {good_sha}" not in out
