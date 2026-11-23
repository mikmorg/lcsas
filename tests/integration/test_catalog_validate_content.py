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
from lcsas.db.schema import create_all
from lcsas.iso.xorriso import SubprocessXorrisoRunner

requires_xorriso = pytest.mark.skipif(
    not shutil.which("xorriso"),
    reason="xorriso not installed",
)

# `make test-integration` selects with `-m integration`; without this the
# module is deselected and runs in no gate target at all (#426).
pytestmark = [pytest.mark.integration, requires_xorriso]


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_minimal_catalog(path: Path, uuid_: str, packs: list[str]) -> None:
    """An LCSAS catalog placing *packs* on the volume *uuid_* (#963:
    validate reads the catalog and finds the volume by its uuid)."""
    conn = sqlite3.connect(str(path))
    create_all(conn)
    conn.execute("INSERT INTO repositories (repo_id, name, mirror_path) "
                 "VALUES ('r', 'r', '/m')")
    volume_id = conn.execute(
        "INSERT INTO volumes (label, uuid, media_type, capacity_bytes) "
        "VALUES ('BURN02_TEST', ?, 'TEST_TINY', 1)", (uuid_,),
    ).lastrowid
    for sha in packs:
        pack_id = conn.execute(
            "INSERT INTO packs (sha256, size_bytes, repo_id) VALUES (?, 1, 'r')",
            (sha,),
        ).lastrowid
        conn.execute("INSERT INTO volume_packs VALUES (?, ?)", (volume_id, pack_id))
    conn.commit()
    conn.close()


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
    _write_minimal_catalog(tree / "catalog.db", "burn02-uuid",
                           [good_sha, bad_sha])
    (tree / "volume_info.json").write_text(
        json.dumps({
            "label": "BURN02_TEST",
            "uuid": "burn02-uuid",
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
